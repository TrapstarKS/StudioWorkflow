local HttpService = game:GetService("HttpService")
local RunService = game:GetService("RunService")
local ScriptEditorService = game:GetService("ScriptEditorService")
local StudioTestService = game:GetService("StudioTestService")

local configuration = __WORKFLOW_CONFIGURATION__

local placeName = configuration.places[tostring(game.PlaceId)]

if not placeName or not RunService:IsEdit() then
    return
end

local session = HttpService:GenerateGUID(false)
local connections = {}
local watched = {}
local identities = {}
local instancesById = {}
local sent = {}
local dirty = {}
local deleted = {}
local knownPaths = {}
local running = true
local initialized = false
local epoch = 0
local changeVersion = 0
local operationResults = {}
local pendingResults = {}
local attemptedOperations = {}
local outboundPaused = false
local serverPaused = false
local pauseUntil = 0
local failures = 0
local obsoleteWork = {}
local pausedWork = {}
local incompleteSnapshot = {}
local lastHeartbeat = 0
local lastSyncWarning = ""
local lastInventory = 0
local wakeSignal = Instance.new("BindableEvent")
local wakePending = false
local wakeWaiting = false

local function signalWake()
    wakePending = true

    if wakeWaiting then
        wakeSignal:Fire()
    end
end

local allowedRoots = {
    Workspace = true,
    ReplicatedFirst = true,
    ReplicatedStorage = true,
    ServerScriptService = true,
    ServerStorage = true,
    StarterPlayer = true,
    StarterGui = true,
    StarterPack = true,
    Lighting = true,
    SoundService = true,
}

local function canEdit()
    return RunService:IsEdit() and StudioTestService.EditModeActive
end

local lastEditing = canEdit()

local function invalidate()
    epoch += 1
    initialized = false
    lastHeartbeat = 0
    signalWake()
end

local function refreshMode()
    local editing = canEdit()

    if editing ~= lastEditing then
        lastEditing = editing
        serverPaused = false
        pauseUntil = 0
        invalidate()
    end

    return editing
end

local function checkEpoch(expectedEpoch, requireEdit)
    local editing = refreshMode()

    if not running or expectedEpoch ~= epoch or (requireEdit and not editing) then
        error(obsoleteWork, 0)
    end
end

local function markDirty(instance)
    changeVersion += 1
    dirty[instance] = changeVersion
end

local function markDeleted(path)
    changeVersion += 1
    deleted[path] = changeVersion
end

local function post(endpoint, payload, expectedEpoch, requireEdit)
    checkEpoch(expectedEpoch, requireEdit)
    payload.place_id = game.PlaceId
    payload.session = session
    payload.protocol_version = 3
    payload.request_id = HttpService:GenerateGUID(false)
    payload.epoch = expectedEpoch
    payload.mode = if canEdit() then "Edit" else "Play"

    local requestTimeout = if endpoint == "/snapshot/finish" then 55 else 8
    local requestDeadline = if endpoint == "/snapshot/finish" then 120 else 55
    local request = {
        Url = configuration.endpoint .. endpoint,
        Method = "POST",
        Headers = {
            ["Content-Type"] = "application/json",
            ["X-Studio-Workflow-Key"] = configuration.key,
        },
        Body = HttpService:JSONEncode(payload),
    }
    local deadline = os.clock() + requestDeadline
    local decoded
    local lastTransportError

    while true do
        checkEpoch(expectedEpoch, requireEdit)
        local remaining = math.floor(deadline - os.clock())

        if remaining < 1 then
            if lastTransportError then
                error("Studio workflow: request retry deadline exceeded: " .. lastTransportError)
            end

            error("Studio workflow: server busy retry deadline exceeded")
        end

        request.Timeout = math.min(requestTimeout, remaining)
        local requestSucceeded, response = pcall(function()
            return HttpService:RequestAsync(request)
        end)
        checkEpoch(expectedEpoch, requireEdit)

        if not requestSucceeded then
            lastTransportError = tostring(response)
            task.wait(math.min(0.2, math.max(0, deadline - os.clock())))
            continue
        end

        lastTransportError = nil

        if not response.Success then
            error("Studio workflow: " .. tostring(response.StatusCode) .. " " .. response.Body)
        end

        decoded = HttpService:JSONDecode(response.Body)

        if typeof(decoded) ~= "table" then
            error("Studio workflow: invalid server response")
        end

        if decoded.retry ~= true then
            break
        end

        if decoded.ok ~= true then
            error("Studio workflow: invalid server retry response")
        end

        local retryAfter = tonumber(decoded.retry_after_seconds) or 0.2

        if retryAfter ~= retryAfter then
            retryAfter = 0.2
        end

        task.wait(math.min(math.clamp(retryAfter, 0.1, 1), math.max(0, deadline - os.clock())))
        checkEpoch(expectedEpoch, requireEdit)
    end

    if decoded.epoch ~= nil and decoded.epoch ~= expectedEpoch then
        invalidate()
        error(obsoleteWork, 0)
    end

    if payload.snapshot_id and decoded.snapshot_id ~= nil and decoded.snapshot_id ~= payload.snapshot_id then
        invalidate()
        error(obsoleteWork, 0)
    end

    if decoded.outbound_paused ~= nil then
        outboundPaused = decoded.outbound_paused == true
    end

    if endpoint == "/operation/result" and decoded.terminal == true and decoded.ok == true then
        if decoded.snapshot_required == true or decoded.complete == false then
            initialized = false
        end

        return decoded
    end

    if decoded.paused or decoded.pause then
        initialized = false
        serverPaused = true
        pauseUntil = os.clock() + math.clamp(tonumber(decoded.retry_after_seconds or decoded.retry_after) or 1, 1, 15)
        error(pausedWork, 0)
    end

    if endpoint == "/heartbeat" then
        serverPaused = false
    end

    if decoded.snapshot_required == true or (decoded.complete == false
        and (endpoint == "/heartbeat" or endpoint == "/changes" or endpoint == "/snapshot/finish")) then
        initialized = false
        error(incompleteSnapshot, 0)
    end

    if decoded.ok == false then
        error("Studio workflow: server rejected " .. endpoint)
    end

    return decoded
end

local function supported(instance)
    if not instance:IsA("LuaSourceContainer") then
        return false
    end

    local ancestor = instance

    while ancestor.Parent and ancestor.Parent ~= game do
        ancestor = ancestor.Parent
    end

    return ancestor.Parent == game and allowedRoots[ancestor.Name] == true
end

local function encodeSegment(name)
    if name == "" then
        return "%00"
    end

    local encoded = name:gsub("[^%w_ %-]", function(character)
        return string.format("%%%02X", string.byte(character))
    end)
    encoded = encoded:gsub(" +$", function(spaces)
        return string.rep("%20", #spaces)
    end)
    local base = string.lower(encoded:match("^[^%.]+") or encoded)

    if base == "con" or base == "prn" or base == "aux" or base == "nul"
        or base:match("^com[1-9]$") or base:match("^lpt[1-9]$") then
        encoded = string.format("%%%02X", string.byte(encoded, 1)) .. string.sub(encoded, 2)
    end

    return encoded
end

local function filename(instance)
    if instance:IsA("ModuleScript") then
        return ".luau"
    end

    if instance:IsA("LocalScript") then
        return ".local.luau"
    end

    local context = instance.RunContext.Name

    if context == "Legacy" then
        return ".legacy.luau"
    end

    return "." .. string.lower(context) .. ".luau"
end

local function paths(instance)
    local segments = {}
    local ancestor = instance
    local ambiguous = false

    while ancestor and ancestor ~= game do
        local segment = encodeSegment(ancestor.Name)
        local ordinal = 0
        local count = 0

        if ancestor.Parent then
            for _, sibling in ancestor.Parent:GetChildren() do
                if sibling.Name ~= ancestor.Name then
                    continue
                end

                count += 1

                if sibling == ancestor then
                    ordinal = count
                end
            end
        end

        if count > 1 then
            segment ..= "~" .. tostring(ordinal)
            ambiguous = true
        end

        table.insert(segments, 1, segment)
        ancestor = ancestor.Parent
    end

    local suffix = filename(instance)
    local base = table.concat(segments, "/")
    return base .. suffix, ambiguous
end

local function capture(instance)
    if not supported(instance) then
        if knownPaths[instance] then
            markDeleted(knownPaths[instance])
            knownPaths[instance] = nil
            sent[instance] = nil
        end

        return nil
    end

    local path, ambiguous = paths(instance)
    local previous = knownPaths[instance]

    if previous and previous ~= path then
        markDeleted(previous)
    end

    knownPaths[instance] = path

    local attributes = {}

    for name, value in instance:GetAttributes() do
        attributes[name] = { kind = typeof(value), value = tostring(value) }
    end

    local metadata = {
        class = instance.ClassName,
        instance_id = identities[instance],
        attributes = attributes,
        tags = instance:GetTags(),
        ambiguous = ambiguous,
        non_script_children = {},
    }

    if instance:IsA("BaseScript") then
        metadata.enabled = instance.Enabled
        metadata.run_context = instance.RunContext.Name
    end

    for _, child in instance:GetChildren() do
        if not child:IsA("LuaSourceContainer") and not child:IsA("Folder") then
            table.insert(metadata.non_script_children, { name = child.Name, class = child.ClassName })
        end
    end

    return {
        path = path,
        instance_path = instance:GetFullName(),
        source = ScriptEditorService:GetEditorSource(instance),
        sync_status = "Bridge",
        metadata = metadata,
    }
end

local function watch(instance)
    if not supported(instance) or watched[instance] then
        return
    end

    local identity = identities[instance] or HttpService:GenerateGUID(false)
    identities[instance] = identity
    instancesById[identity] = instance
    markDirty(instance)

    watched[instance] = {
        instance.Changed:Connect(function(property)
            if property == "Source" or property == "Name" or property == "Parent"
                or property == "Enabled" or property == "RunContext" then
                markDirty(instance)
            end
        end),
        instance.AttributeChanged:Connect(function()
            markDirty(instance)
        end),
    }
end

local function sendBatches(endpoint, records, expectedEpoch, snapshotId)
    local batch = {}
    local bytes = 0

    for _, record in records do
        checkEpoch(expectedEpoch, true)
        local serialized = HttpService:JSONEncode(record)
        local length = #serialized

        if length > 16000000 then
            error("StudioWorkflow: script record exceeds the 16 MB bridge limit; split it in Studio")
        end

        if bytes + length > 450000 and #batch > 0 then
            post(endpoint, { scripts = batch, snapshot_id = snapshotId }, expectedEpoch, true)
            batch = {}
            bytes = 0
        end

        if length > 700000 then
            local chunks = {}
            local offset = 1

            while offset <= length do
                local ending = math.min(offset + 199999, length)

                while ending < length do
                    local nextByte = string.byte(serialized, ending + 1)

                    if nextByte < 128 or nextByte >= 192 then
                        break
                    end

                    ending -= 1
                end

                table.insert(chunks, string.sub(serialized, offset, ending))
                offset = ending + 1
            end

            local transfer = HttpService:GenerateGUID(false)

            for position, text in chunks do
                post("/record/chunk", {
                    endpoint = endpoint,
                    transfer = transfer,
                    position = position,
                    total = #chunks,
                    text = text,
                    snapshot_id = snapshotId,
                }, expectedEpoch, true)
            end

            continue
        end

        table.insert(batch, record)
        bytes += length
    end

    if #batch > 0 then
        post(endpoint, { scripts = batch, snapshot_id = snapshotId }, expectedEpoch, true)
    end
end

local function fullSnapshot(expectedEpoch)
    checkEpoch(expectedEpoch, true)
    local records = {}
    local snapshotSent = {}
    local snapshotDirty = {}
    local snapshotDeleted = table.clone(deleted)
    local snapshotId = HttpService:GenerateGUID(false)

    for _, instance in game:GetDescendants() do
        if not supported(instance) then
            continue
        end

        watch(instance)
        snapshotDirty[instance] = dirty[instance]
        local record = capture(instance)

        if record then
            table.insert(records, record)
            snapshotSent[instance] = { path = record.path, source = record.source, metadata = HttpService:JSONEncode(record.metadata) }
        end
    end

    post("/snapshot/start", { count = #records, snapshot_id = snapshotId }, expectedEpoch, true)
    sendBatches("/snapshot/batch", records, expectedEpoch, snapshotId)
    post("/snapshot/finish", { snapshot_id = snapshotId }, expectedEpoch, true)

    for instance, version in snapshotDirty do
        if dirty[instance] == version then
            dirty[instance] = nil
        end
    end

    for path, version in snapshotDeleted do
        if deleted[path] == version then
            deleted[path] = nil
        end
    end

    sent = {}

    for instance, record in snapshotSent do
        if watched[instance] then
            sent[instance] = record
        end
    end

    initialized = true
    lastInventory = os.clock()
end

local function flush(expectedEpoch)
    checkEpoch(expectedEpoch, true)
    local pending = table.clone(dirty)
    local acknowledged = {}
    local records = {}

    for instance in pending do
        local record = capture(instance)

        if record then
            local previous = sent[instance]
            local metadata = HttpService:JSONEncode(record.metadata)

            if not previous or previous.path ~= record.path or previous.source ~= record.source or previous.metadata ~= metadata then
                table.insert(records, record)
                acknowledged[instance] = { path = record.path, source = record.source, metadata = metadata }
            end
        end
    end

    local livePaths = {}

    for _, path in knownPaths do
        livePaths[path] = true
    end

    local removed = {}
    local pendingDeleted = table.clone(deleted)

    for path in pendingDeleted do
        if not livePaths[path] then
            table.insert(removed, path)
        end
    end

    if #removed > 0 then
        post("/changes", { deleted = removed }, expectedEpoch, true)
    end

    sendBatches("/changes", records, expectedEpoch)

    for instance, record in acknowledged do
        if watched[instance] then
            sent[instance] = record
        end
    end

    for instance, version in pending do
        if dirty[instance] == version then
            dirty[instance] = nil
        end
    end

    for path, version in pendingDeleted do
        if deleted[path] == version then
            deleted[path] = nil
        end
    end
end

local function sendPendingResults(expectedEpoch)
    for operationId, result in pendingResults do
        post("/operation/result", result, expectedEpoch, true)
        pendingResults[operationId] = nil
    end
end

local function performOperation(operation, expectedEpoch, attempt)
    checkEpoch(expectedEpoch, true)

    if not initialized then
        error(incompleteSnapshot, 0)
    end

    if typeof(operation.id) ~= "string" or typeof(operation.instance_id) ~= "string" or typeof(operation.path) ~= "string" then
        error("Studio workflow: invalid operation identity")
    end

    if operationResults[operation.id] then
        pendingResults[operation.id] = operationResults[operation.id]
        sendPendingResults(expectedEpoch)
        return
    end

    if outboundPaused then
        error(pausedWork, 0)
    end

    if operation.chunks then
        if typeof(operation.chunks) ~= "number" or operation.chunks % 1 ~= 0 or operation.chunks < 1 or operation.chunks > 1000 then
            error("Studio workflow: invalid operation chunk count")
        end

        local chunks = {}

        for position = 1, operation.chunks do
            local chunk = post("/operation/chunk", { id = operation.id, position = position }, expectedEpoch, true)

            if chunk.id ~= operation.id or chunk.position ~= position or chunk.total ~= operation.chunks or typeof(chunk.text) ~= "string" then
                error("Studio workflow: inconsistent operation chunk")
            end

            table.insert(chunks, chunk.text)
        end

        local content = HttpService:JSONDecode(table.concat(chunks))

        if typeof(content) ~= "table" or typeof(content.baseline) ~= "string" or typeof(content.source) ~= "string" then
            error("Studio workflow: invalid operation content")
        end

        operation.baseline = content.baseline
        operation.source = content.source
    end

    if typeof(operation.baseline) ~= "string" or typeof(operation.source) ~= "string" then
        error("Studio workflow: invalid operation source")
    end

    checkEpoch(expectedEpoch, true)
    local instance = instancesById[operation.instance_id]
    local outcome = "rejected"
    local reason = "Script identity or path changed"

    if instance and supported(instance) and identities[instance] == operation.instance_id and paths(instance) == operation.path then
        reason = "Studio source changed after the local edit began"

        ScriptEditorService:UpdateSourceAsync(instance, function(current)
            if not running or expectedEpoch ~= epoch or not canEdit() or outboundPaused or not supported(instance)
                or identities[instance] ~= operation.instance_id or instancesById[operation.instance_id] ~= instance
                or paths(instance) ~= operation.path or current ~= operation.baseline then
                return nil
            end

            attempt.writeAttempted = true
            return operation.source
        end)

        checkEpoch(expectedEpoch, true)

        if identities[instance] == operation.instance_id and supported(instance)
            and paths(instance) == operation.path and ScriptEditorService:GetEditorSource(instance) == operation.source then
            outcome = "applied"
            reason = "Source verified in the Studio editor"
        end

        markDirty(instance)
    end

    local result = { id = operation.id, outcome = outcome, details = { message = reason } }
    operationResults[operation.id] = result
    pendingResults[operation.id] = result
    flush(expectedEpoch)
    sendPendingResults(expectedEpoch)
end

local function applyOperation(operation, expectedEpoch)
    if typeof(operation) ~= "table" or typeof(operation.id) ~= "string" then
        error("Studio workflow: invalid operation")
    end

    local attempt = attemptedOperations[operation.id]

    if attempt and attempt.writeAttempted and not operationResults[operation.id] then
        error("Studio workflow: previous source update has an uncertain outcome")
    end

    attempt = { writeAttempted = false }
    attemptedOperations[operation.id] = attempt
    local success, message = pcall(performOperation, operation, expectedEpoch, attempt)

    if success then
        attemptedOperations[operation.id] = nil
        return
    end

    if running and not attempt.writeAttempted and not operationResults[operation.id] then
        local code = "transfer_interrupted"

        if not canEdit() then
            code = "play_mode"
        elseif expectedEpoch ~= epoch then
            code = "session_changed"
        elseif message == incompleteSnapshot then
            code = "snapshot_incomplete"
        end

        local result = {
            id = operation.id,
            outcome = "rejected",
            details = { message = "Operation interrupted before a source write", code = code, write_attempted = false },
        }
        operationResults[operation.id] = result
        pendingResults[operation.id] = result
    end

    if operationResults[operation.id] then
        attemptedOperations[operation.id] = nil
    end

    error(message, 0)
end

table.insert(connections, game.DescendantAdded:Connect(function(instance)
    watch(instance)
end))

table.insert(connections, game.DescendantRemoving:Connect(function(instance)
    if not watched[instance] then
        return
    end

    if knownPaths[instance] then
        markDeleted(knownPaths[instance])
        knownPaths[instance] = nil
    end

    for _, connection in watched[instance] do
        connection:Disconnect()
    end

    instancesById[identities[instance]] = nil
    identities[instance] = nil
    watched[instance] = nil
    sent[instance] = nil
    dirty[instance] = nil
end))

table.insert(connections, ScriptEditorService.TextDocumentDidChange:Connect(function(document)
    local instance = document:GetScript()

    if instance and watched[instance] then
        markDirty(instance)
    end
end))

table.insert(connections, StudioTestService:GetPropertyChangedSignal("EditModeActive"):Connect(function()
    lastEditing = canEdit()
    serverPaused = false
    pauseUntil = 0
    invalidate()
end))

local function stop()
    if not running then
        return
    end

    running = false
    invalidate()

    for _, connection in connections do
        connection:Disconnect()
    end

    table.clear(connections)

    for _, instanceConnections in watched do
        for _, connection in instanceConnections do
            connection:Disconnect()
        end
    end

    table.clear(watched)
end

if plugin then
    table.insert(connections, plugin.Unloading:Connect(stop))
end

local controller = {
    session = session,
    place = placeName,
    stop = stop,
    transportStatus = function()
        return {
            transport = "http",
            heartbeatInterval = 1,
            retryFailures = failures,
        }
    end,
}
local controllerKey = configuration.controller_key or "StudioWorkflow"

if _G[controllerKey] then
    _G[controllerKey].stop()
end

_G[controllerKey] = controller

task.spawn(function()
    while running do
        refreshMode()
        local expectedEpoch = epoch
        wakePending = false

        local success, message = pcall(function()
            if not canEdit() then
                post("/heartbeat", {}, expectedEpoch, false)
                return
            end

            if serverPaused then
                if os.clock() < pauseUntil then
                    return
                end

                post("/heartbeat", {}, expectedEpoch, true)
                return
            end

            sendPendingResults(expectedEpoch)

            if not initialized then
                fullSnapshot(expectedEpoch)
            end

            if os.clock() - lastInventory > 15 then
                for _, instance in game:GetDescendants() do
                    watch(instance)
                end

                for instance in watched do
                    markDirty(instance)
                end

                lastInventory = os.clock()
            end

            if next(dirty) or next(deleted) then
                flush(expectedEpoch)
            end

            if os.clock() - lastHeartbeat >= 1 then
                local response = post("/heartbeat", { operation_chunks = true }, expectedEpoch, true)
                local syncWarning = table.concat(response.sync_issues or {}, "\n")

                if syncWarning ~= lastSyncWarning then
                    if syncWarning ~= "" then
                        warn("StudioWorkflow needs attention:\n" .. syncWarning)
                    end

                    lastSyncWarning = syncWarning
                end

                if not outboundPaused then
                    for _, operation in response.operations or {} do
                        applyOperation(operation, expectedEpoch)
                    end
                end

                lastHeartbeat = os.clock()
            end
        end)

        local failed = not success and message ~= obsoleteWork and message ~= pausedWork and message ~= incompleteSnapshot

        if failed and running and expectedEpoch == epoch then
            invalidate()
            failures += 1

            if failures == 1 or failures % 12 == 0 then
                warn(tostring(message))
            end
        elseif success then
            failures = 0
        end

        if not running then
            break
        end

        if wakePending and not failed then
            continue
        end

        wakePending = false
        wakeWaiting = true

        local timeout

        timeout = task.delay(if failed then math.min(2 ^ math.min(failures, 3), 8) else 1, function()
            timeout = nil

            if wakeWaiting then
                wakeSignal:Fire()
            end
        end)

        wakeSignal.Event:Wait()
        wakeWaiting = false
        if timeout then
            task.cancel(timeout)
        end
    end

    wakeSignal:Destroy()
end)

return controller
