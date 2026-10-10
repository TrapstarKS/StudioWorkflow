local fs = require("@lune/fs")
local process = require("@lune/process")
local luau = require("@lune/luau")
local serde = require("@lune/serde")

local observer = fs.readFile(process.args[1] or "studioworkflow/observer.luau")
luau.compile(observer)

local loadObserver = luau.load([[
local fixture = ...
local game = fixture.game
local task = fixture.task
local Instance = fixture.Instance
local plugin = fixture.plugin
local _G = fixture.globals
local os = { clock = function() return fixture.now end }
local warn = function(message) table.insert(fixture.warnings, message) end
local __WORKFLOW_CONFIGURATION__ = fixture.configuration
]] .. observer)

local function resumeThread(thread, ...)
    local success, message = coroutine.resume(thread, ...)
    assert(success, message)
end

local function newFixture()
    local fixture = {
        now = 100,
        sequence = 0,
        scheduled = {},
        cancelled = {},
        globals = {},
        requests = {},
        requestResponses = {},
        warnings = {},
        operations = {},
        operationChunks = {},
        results = {},
        mirror = {},
        staging = {},
        complete = false,
        outboundPaused = false,
        runEdit = true,
        writes = 0,
        updateCalls = 0,
        connected = 0,
        destroyedEvents = 0,
        configuration = { endpoint = "http://fixture", key = "fixture-only", places = { ["1"] = "main" }, websocket_wakeup = false },
    }

    local function schedule(thread, delay, arguments)
        fixture.sequence += 1
        table.insert(fixture.scheduled, { thread = thread, time = fixture.now + delay, sequence = fixture.sequence, arguments = arguments or {} })
    end

    fixture.task = {
        spawn = function(callback)
            local thread = coroutine.create(callback)
            resumeThread(thread)
            return thread
        end,
        delay = function(delay, callback)
            local thread = coroutine.create(callback)
            schedule(thread, delay)
            return thread
        end,
        cancel = function(thread)
            fixture.cancelled[thread] = true
        end,
        wait = function(delay)
            schedule(coroutine.running(), delay)
            return coroutine.yield()
        end,
    }

    function fixture:advance(duration)
        local target = self.now + duration
        local iterations = 0

        while true do
            local nextIndex = nil
            local nextItem = nil

            for index, item in self.scheduled do
                if not self.cancelled[item.thread] and item.time <= target
                    and (not nextItem or item.time < nextItem.time or (item.time == nextItem.time and item.sequence < nextItem.sequence)) then
                    nextIndex = index
                    nextItem = item
                end
            end

            if not nextItem then
                break
            end

            iterations += 1
            assert(iterations < 10000, "Observer scheduler did not settle")
            table.remove(self.scheduled, nextIndex)
            self.now = nextItem.time
            resumeThread(nextItem.thread, table.unpack(nextItem.arguments))
        end

        self.now = target
    end

    local function signal()
        local event = { callbacks = {}, waiters = {}, destroyed = false }

        function event:Connect(callback)
            local connection = { Connected = true, callback = callback }
            fixture.connected += 1

            function connection:Disconnect()
                if self.Connected then
                    self.Connected = false
                    fixture.connected -= 1
                end
            end

            table.insert(self.callbacks, connection)
            return connection
        end

        function event:Fire(...)
            assert(not self.destroyed, "Destroyed event was fired")

            for _, connection in table.clone(self.callbacks) do
                if connection.Connected then
                    connection.callback(...)
                end
            end

            local waiters = self.waiters
            self.waiters = {}

            for _, thread in waiters do
                schedule(thread, 0, { ... })
            end
        end

        function event:Wait()
            table.insert(self.waiters, coroutine.running())
            return coroutine.yield()
        end

        return event
    end

    fixture.Instance = {
        new = function(className)
            assert(className == "BindableEvent", "Observer created a real instance")
            local event = signal()

            return {
                Event = event,
                Fire = function(_, ...) event:Fire(...) end,
                Destroy = function()
                    assert(not event.destroyed, "Event destroyed twice")
                    event.destroyed = true
                    fixture.destroyedEvents += 1
                end,
            }
        end,
    }

    local function node(className, name, parent, source)
        local instance = {
            ClassName = className,
            Name = name,
            Parent = parent,
            Source = source or "",
            children = {},
            Changed = signal(),
            AttributeChanged = signal(),
        }

        function instance:IsA(expected)
            return self.ClassName == expected or (expected == "LuaSourceContainer" and self.ClassName == "ModuleScript")
        end

        function instance:GetChildren()
            return table.clone(self.children)
        end

        function instance:GetAttributes()
            return {}
        end

        function instance:GetTags()
            return {}
        end

        function instance:GetFullName()
            local names = { self.Name }
            local ancestor = self.Parent

            while ancestor and ancestor ~= fixture.game do
                table.insert(names, 1, ancestor.Name)
                ancestor = ancestor.Parent
            end

            return table.concat(names, ".")
        end

        if parent then
            table.insert(parent.children, instance)
        end

        return instance
    end

    fixture.game = node("DataModel", "game")
    fixture.game.PlaceId = 1
    fixture.game.DescendantAdded = signal()
    fixture.game.DescendantRemoving = signal()
    fixture.root = node("ReplicatedStorage", "ReplicatedStorage", fixture.game)
    fixture.script = node("ModuleScript", "Example", fixture.root, "return 'base'")
    fixture.modeChanged = signal()
    fixture.studio = { EditModeActive = true }
    fixture.plugin = { Unloading = signal() }

    function fixture.game:GetDescendants()
        local descendants = {}

        local function collect(parent)
            for _, child in parent.children do
                table.insert(descendants, child)
                collect(child)
            end
        end

        collect(self)
        return descendants
    end

    function fixture.studio:GetPropertyChangedSignal(property)
        assert(property == "EditModeActive")
        return fixture.modeChanged
    end

    function fixture:setMode(editing)
        self.studio.EditModeActive = editing
        self.modeChanged:Fire()
    end

    function fixture:setSource(instance, source)
        instance.Source = source
        instance.Changed:Fire("Source")
    end

    function fixture:add(name, source)
        local instance = node("ModuleScript", name, self.root, source)
        self.game.DescendantAdded:Fire(instance)
        return instance
    end

    function fixture:folder(name, parent)
        return node("Folder", name, parent)
    end

    function fixture:move(instance, parent)
        table.remove(instance.Parent.children, assert(table.find(instance.Parent.children, instance)))
        instance.Parent = parent
        table.insert(parent.children, instance)
        instance.Changed:Fire("Parent")
    end

    function fixture:remove(instance)
        self.game.DescendantRemoving:Fire(instance)
        local index = table.find(instance.Parent.children, instance)
        table.remove(instance.Parent.children, index)
        instance.Parent = nil
        instance.Changed:Fire("Parent")
    end

    function fixture:reattach(instance)
        instance.Parent = self.root
        table.insert(self.root.children, instance)
        self.game.DescendantAdded:Fire(instance)
    end

    local editor = { TextDocumentDidChange = signal() }

    function editor:GetEditorSource(instance)
        local source = instance.Source

        if fixture.removeOnCapture == instance then
            fixture.removeOnCapture = nil
            fixture:remove(instance)
        end

        return source
    end

    function editor:UpdateSourceAsync(instance, callback)
        fixture.updateCalls += 1

        if fixture.beforeWrite then
            fixture.beforeWrite(instance)
        end

        local replacement = callback(instance.Source)

        if replacement then
            fixture.writes += 1
            fixture:setSource(instance, replacement)
        end

        if fixture.afterWrite then
            fixture.afterWrite(instance)
        end
    end

    local http = {}
    local identities = 0

    function http:GenerateGUID()
        identities += 1
        return "fixture-id-" .. tostring(identities)
    end

    function http:JSONEncode(value)
        return serde.encode("json", value)
    end

    function http:JSONDecode(value)
        return serde.decode("json", value)
    end

    local function serverResponse(endpoint, payload)
        local response = { ok = true, epoch = payload.epoch }

        if payload.snapshot_id then
            response.snapshot_id = payload.snapshot_id
        end

        if endpoint == "/snapshot/start" then
            fixture.complete = false
            fixture.staging = {}
        elseif endpoint == "/snapshot/batch" then
            for _, record in payload.scripts do
                fixture.staging[record.path] = record
            end
        elseif endpoint == "/snapshot/finish" then
            fixture.mirror = fixture.staging
            fixture.complete = true
            response.complete = true
        elseif endpoint == "/changes" then
            for _, record in payload.scripts or {} do
                fixture.mirror[record.path] = record
            end

            for _, path in payload.deleted or {} do
                fixture.mirror[path] = nil
            end
        elseif endpoint == "/heartbeat" then
            response.complete = fixture.complete
            response.outbound_paused = fixture.outboundPaused
            response.operations = {}

            if fixture.complete and payload.mode == "Edit" and not fixture.outboundPaused then
                response.operations = fixture.operations
                fixture.operations = {}
            end
        elseif endpoint == "/operation/chunk" then
            response = table.clone(assert(fixture.operationChunks[payload.id])[payload.position])
        elseif endpoint == "/operation/result" then
            fixture.results[payload.id] = payload
        else
            error("Unexpected request: " .. endpoint)
        end

        return response
    end

    function http:RequestAsync(request)
        local endpoint = string.sub(request.Url, #fixture.configuration.endpoint + 1)
        local maxTimeout = if endpoint == "/snapshot/finish" then 55 else 8
        assert(request.Timeout >= 1 and request.Timeout <= maxTimeout and request.Timeout % 1 == 0, "Invalid native timeout")
        local payload = self:JSONDecode(request.Body)
        assert(payload.protocol_version == 3, "Missing observer protocol handshake")
        assert(typeof(payload.request_id) == "string" and payload.request_id ~= "", "Missing request identity")
        assert(typeof(payload.epoch) == "number" and payload.epoch >= 0 and payload.epoch % 1 == 0, "Invalid epoch")
        table.insert(fixture.requests, { endpoint = endpoint, payload = payload, time = fixture.now, body = request.Body, timeout = request.Timeout })

        if fixture.beforeRequest then
            fixture.beforeRequest(endpoint, payload)
        end

        if fixture.busyResponse then
            local busy = fixture.busyResponse(endpoint, payload)

            if busy then
                return { Success = true, StatusCode = 200, Body = self:JSONEncode(busy) }
            end
        end

        local cached = fixture.requestResponses[payload.request_id]

        if cached then
            assert(cached.body == request.Body, "Request identity changed content")
        end

        local response = if cached then nil else serverResponse(endpoint, payload)

        if response and fixture.responseHook then
            response = fixture.responseHook(endpoint, payload, response) or response
        end

        local result = if cached then cached.result else { Success = true, StatusCode = 200, Body = self:JSONEncode(response) }

        if not cached then
            fixture.requestResponses[payload.request_id] = { body = request.Body, result = result }
        end

        if fixture.holdRoute ~= endpoint then
            return result
        end

        fixture.holdRoute = nil
        assert(not fixture.held, "Overlapping HTTP requests")
        local held = { thread = coroutine.running(), response = result }
        fixture.held = held
        held.timeout = fixture.task.delay(request.Timeout, function()
            if fixture.held == held then
                fixture.held = nil
                resumeThread(held.thread, false)
            end
        end)

        local released = coroutine.yield()

        if not released then
            error("Simulated native HTTP timeout")
        end

        return held.response
    end

    local services = {
        HttpService = http,
        RunService = { IsEdit = function() return fixture.runEdit end },
        StudioTestService = fixture.studio,
        ScriptEditorService = editor,
    }

    function fixture.game:GetService(name)
        return assert(services[name], "Unexpected service")
    end

    function fixture:start()
        self.controller = loadObserver(self)
        return self.controller
    end

    function fixture:release()
        local held = assert(self.held, "No suspended HTTP request")
        self.held = nil
        self.task.cancel(held.timeout)
        schedule(held.thread, 0, { true })
        self:advance(0)
    end

    function fixture:operation(source, chunked)
        local record = assert(self.mirror["ReplicatedStorage/Example.luau"])
        local operation = {
            id = "operation-" .. tostring(#self.requests + #self.operations + 1),
            instance_id = record.metadata.instance_id,
            path = record.path,
            baseline = self.script.Source,
            source = source,
        }

        if chunked then
            local serialized = serde.encode("json", { baseline = operation.baseline, source = source })
            local middle = math.floor(#serialized / 2)
            operation.baseline = nil
            operation.source = nil
            operation.chunks = 2
            self.operationChunks[operation.id] = {
                { id = operation.id, position = 1, total = 2, text = string.sub(serialized, 1, middle) },
                { id = operation.id, position = 2, total = 2, text = string.sub(serialized, middle + 1) },
            }
        end

        table.insert(self.operations, operation)
        return operation
    end

    function fixture:count(endpoint)
        local total = 0

        for _, request in self.requests do
            if request.endpoint == endpoint then
                total += 1
            end
        end

        return total
    end

    function fixture:close()
        self.globals.StudioWorkflow.stop()
        self:advance(0)

        if self.held then
            self:release()
        end

        self:advance(20)
        assert(self.connected == 0, "Observer leaked event connections")
        assert(self.destroyedEvents == 1, "Observer did not release its wake event")
    end

    return fixture
end

local passed = 0

local function test(name, callback)
    local fixture = newFixture()
    callback(fixture)
    fixture:close()
    passed += 1
    print("PASS " .. name)
end

test("normal inventory and exact-base application", function(fixture)
    fixture:start()
    assert(fixture.mirror["ReplicatedStorage/Example.luau"].source == "return 'base'")
    local operation = fixture:operation("return 'local'")
    fixture:advance(1)
    assert(fixture.writes == 1 and fixture.results[operation.id].outcome == "applied")
    assert(fixture.mirror[operation.path].source == "return 'local'")
end)

test("dirty edit during a suspended changes request survives its ACK", function(fixture)
    fixture:start()
    fixture:setSource(fixture.script, "return 'first'")
    fixture.holdRoute = "/changes"
    fixture:advance(1)
    assert(fixture.held)
    fixture:setSource(fixture.script, "return 'latest'")
    fixture:release()
    fixture:advance(1)
    assert(fixture.mirror["ReplicatedStorage/Example.luau"].source == "return 'latest'")
    assert(fixture:count("/snapshot/start") == 1)
end)

test("deletion during a suspended changes request survives its ACK", function(fixture)
    local removed = fixture:add("Removed", "return 2")
    fixture:start()
    fixture:setSource(fixture.script, "return 'first'")
    fixture.holdRoute = "/changes"
    fixture:advance(1)
    fixture:remove(removed)
    fixture:release()
    fixture:advance(1)
    assert(fixture.mirror["ReplicatedStorage/Removed.luau"] == nil)
end)

test("dirty, added and removed scripts during snapshot remain pending", function(fixture)
    local removed = fixture:add("Removed", "return 2")
    fixture.holdRoute = "/snapshot/batch"
    fixture:start()
    fixture:setSource(fixture.script, "return 'latest'")
    fixture:remove(removed)
    fixture:add("Added", "return 3")
    fixture:release()
    fixture:advance(1)
    assert(fixture.mirror["ReplicatedStorage/Example.luau"].source == "return 'latest'")
    assert(fixture.mirror["ReplicatedStorage/Removed.luau"] == nil)
    assert(fixture.mirror["ReplicatedStorage/Added.luau"].source == "return 3")
end)

test("deletion during capture survives the snapshot acknowledgment", function(fixture)
    local removed = fixture:add("Removed", "return 2")
    fixture.removeOnCapture = removed
    fixture:start()
    fixture:advance(1)
    assert(fixture.mirror["ReplicatedStorage/Removed.luau"] == nil)
    assert(fixture:count("/changes") > 0)
end)

test("reserved names and trailing spaces have distinct portable paths", function(fixture)
    fixture:add("CON", "return 2")
    fixture:add("COM1", "return 3")
    fixture:add("Example ", "return 4")
    fixture:add("Example.", "return 5")
    fixture:start()
    assert(fixture.mirror["ReplicatedStorage/%43ON.luau"].source == "return 2")
    assert(fixture.mirror["ReplicatedStorage/%43OM1.luau"].source == "return 3")
    assert(fixture.mirror["ReplicatedStorage/Example%20.luau"].source == "return 4")
    assert(fixture.mirror["ReplicatedStorage/Example%2E.luau"].source == "return 5")
    assert(fixture.mirror["ReplicatedStorage/Example.luau"].source == "return 'base'")
end)

test("heartbeat complete=false requires another inventory", function(fixture)
    local intercepted = false
    fixture.responseHook = function(endpoint, _, response)
        if endpoint == "/heartbeat" and not intercepted then
            intercepted = true
            fixture.complete = false
            response.complete = false
            response.operations = { { id = "obsolete-operation" } }
        end
    end
    fixture:start()
    fixture:advance(2)
    assert(fixture:count("/snapshot/start") == 2 and fixture.writes == 0)
end)

test("snapshot finish complete=false never enables operation writes", function(fixture)
    local intercepted = false
    fixture.responseHook = function(endpoint, _, response)
        if endpoint == "/snapshot/finish" and not intercepted then
            intercepted = true
            fixture.complete = false
            response.complete = false
        end
    end
    fixture:start()
    assert(fixture:count("/heartbeat") == 0)
    fixture:advance(2)
    assert(fixture:count("/snapshot/start") == 2)
end)

test("snapshot_required is honored independently of complete", function(fixture)
    fixture:start()
    local intercepted = false
    fixture.responseHook = function(endpoint, _, response)
        if endpoint == "/heartbeat" and not intercepted then
            intercepted = true
            response.snapshot_required = true
        end
    end
    fixture:advance(3)
    assert(fixture:count("/snapshot/start") == 2)
end)

test("outbound pause blocks writes and still mirrors Studio edits", function(fixture)
    fixture.outboundPaused = true
    fixture:start()
    fixture:setSource(fixture.script, "return 'teammate'")
    fixture.responseHook = function(endpoint, _, response)
        if endpoint == "/heartbeat" then
            response.operations = { { id = "must-not-run" } }
        end
    end
    fixture:advance(2)
    assert(fixture.writes == 0 and fixture.updateCalls == 0)
    assert(fixture.mirror["ReplicatedStorage/Example.luau"].source == "return 'teammate'")
    fixture.responseHook = nil
    fixture.outboundPaused = false
    fixture:operation("return 'resumed'")
    fixture:advance(1)
    assert(fixture.writes == 1)
end)

test("server pause retry hint is bounded and resumes inventory", function(fixture)
    local intercepted = false
    fixture.responseHook = function(endpoint, _, response)
        if endpoint == "/snapshot/start" and not intercepted then
            intercepted = true
            response.paused = true
            response.retry_after_seconds = 3
        end
    end
    fixture:start()
    fixture:advance(2)
    assert(#fixture.requests == 1)
    fixture:advance(3)
    assert(fixture.complete and fixture:count("/snapshot/start") == 2)
end)

for _, endpoint in { "/snapshot/start", "/snapshot/batch", "/snapshot/finish" } do
    test("rapid Play/Edit fences old " .. endpoint .. " response", function(fixture)
        fixture.holdRoute = endpoint
        fixture:start()
        fixture:setMode(false)
        fixture:setMode(true)
        fixture:setSource(fixture.script, "return 'after-play'")
        local requestCount = #fixture.requests
        fixture:release()
        fixture:advance(1)
        assert(fixture.requests[requestCount + 1].endpoint == "/snapshot/start")
        assert(fixture.requests[requestCount + 1].payload.epoch == 2)
        assert(fixture.mirror["ReplicatedStorage/Example.luau"].source == "return 'after-play'")
        assert(fixture.writes == 0)
    end)
end

test("rapid Play/Edit discards operations in an old heartbeat", function(fixture)
    fixture:start()
    fixture:operation("return 'obsolete'")
    fixture.holdRoute = "/heartbeat"
    fixture:advance(1)
    fixture:setMode(false)
    fixture:setMode(true)
    fixture:release()
    fixture:advance(1)
    assert(fixture.writes == 0 and fixture.script.Source == "return 'base'")
    assert(fixture:count("/snapshot/start") == 2)
end)

for _, endpoint in { "/snapshot/start", "/snapshot/batch", "/snapshot/finish", "/heartbeat" } do
    test("stop discards outstanding " .. endpoint .. " response", function(fixture)
        fixture.holdRoute = endpoint
        fixture:start()
        local count = #fixture.requests
        fixture.globals.StudioWorkflow.stop()
        fixture:release()
        fixture:advance(10)
        fixture.globals.StudioWorkflow.stop()
        assert(#fixture.requests == count and fixture.writes == 0)
    end)
end

test("stop interrupts network backoff and is idempotent", function(fixture)
    fixture.beforeRequest = function() error("VPN unavailable") end
    fixture:start()
    fixture.globals.StudioWorkflow.stop()
    fixture:advance(10)
    fixture.globals.StudioWorkflow.stop()
    assert(#fixture.requests == 1)
end)

test("concurrent source edit wins inside UpdateSourceAsync", function(fixture)
    fixture:start()
    local operation = fixture:operation("return 'local'")
    fixture.beforeWrite = function(instance) fixture:setSource(instance, "return 'teammate'") end
    fixture:advance(1)
    assert(fixture.writes == 0 and fixture.script.Source == "return 'teammate'")
    assert(fixture.results[operation.id].outcome == "rejected")
    assert(fixture.results[operation.id].details.code == nil)
end)

test("identity is rechecked inside UpdateSourceAsync", function(fixture)
    fixture:start()
    local operation = fixture:operation("return 'local'")
    fixture.beforeWrite = function(instance)
        fixture:remove(instance)
        fixture:reattach(instance)
    end
    fixture:advance(1)
    assert(fixture.writes == 0 and fixture.script.Source == "return 'base'")
    assert(fixture.results[operation.id].outcome == "rejected")
end)

test("rapid callback mode change reports no-write transient result", function(fixture)
    fixture:start()
    local operation = fixture:operation("return 'local'")
    fixture.beforeWrite = function()
        fixture:setMode(false)
        fixture:setMode(true)
    end
    fixture:advance(2)
    local result = assert(fixture.results[operation.id])
    assert(fixture.writes == 0 and result.details.write_attempted == false)
    assert(result.details.code == "session_changed")
end)

test("chunk network failure retries before applying the source once", function(fixture)
    fixture:start()
    local operation = fixture:operation("return 'chunked'", true)
    local intercepted = false
    fixture.beforeRequest = function(endpoint, payload)
        if endpoint == "/operation/chunk" and payload.position == 2 and not intercepted then
            intercepted = true
            error("VPN interrupted chunk")
        end
    end
    fixture:advance(4)
    local result = assert(fixture.results[operation.id])
    assert(fixture.updateCalls == 1 and fixture.writes == 1)
    assert(result.outcome == "applied" and fixture:count("/operation/chunk") == 3)
    assert(#fixture.warnings == 0)
end)

test("lost result ACK retries the result without another source write", function(fixture)
    fixture:start()
    local operation = fixture:operation("return 'local'")
    local intercepted = false
    fixture.responseHook = function(endpoint)
        if endpoint == "/operation/result" and not intercepted then
            intercepted = true
            error("Result ACK dropped by VPN")
        end
    end
    fixture:advance(4)
    assert(fixture.results[operation.id].outcome == "applied")
    assert(fixture:count("/operation/result") == 2 and fixture.writes == 1 and fixture.updateCalls == 1)
end)

test("source echo transport failure retries without resnapshot", function(fixture)
    fixture:start()
    local operation = fixture:operation("return 'local'")
    local intercepted = false
    fixture.beforeRequest = function(endpoint)
        if endpoint == "/changes" and fixture.writes == 1 and not intercepted then
            intercepted = true
            error("Source echo failed")
        end
    end
    fixture:advance(4)
    assert(fixture.results[operation.id].outcome == "applied" and fixture.writes == 1)
    local changeBodies = {}
    local recovered = false

    for _, request in fixture.requests do
        if request.endpoint == "/changes" then
            table.insert(changeBodies, request.body)
        elseif request.endpoint == "/snapshot/start" and request.payload.epoch > 0 then
            recovered = true
        end
    end

    assert(#changeBodies == 2 and changeBodies[1] == changeBodies[2] and not recovered)
    assert(fixture.mirror[operation.path].source == "return 'local'")
end)

test("duplicate operation ID resends confirmation only", function(fixture)
    fixture:start()
    local operation = fixture:operation("return 'local'")
    fixture:advance(1)
    table.insert(fixture.operations, table.clone(operation))
    fixture:advance(1)
    assert(fixture.writes == 1 and fixture.updateCalls == 1)
    assert(fixture:count("/operation/result") == 2)
end)

test("native timeout recovers with one outstanding HTTP request", function(fixture)
    fixture:start()
    fixture:operation("return 'obsolete'")
    fixture.holdRoute = "/heartbeat"
    fixture:advance(1)
    local count = #fixture.requests
    fixture:advance(7)
    assert(fixture.held and #fixture.requests == count)
    fixture:advance(4)
    assert(not fixture.held and fixture:count("/snapshot/start") == 1)
    assert(fixture.writes == 1 and #fixture.warnings == 0)
end)

test("terminal result ACK can require a snapshot without an ACK retry loop", function(fixture)
    fixture:start()
    local operation = fixture:operation("return 'local'")
    fixture.responseHook = function(endpoint, _, response)
        if endpoint == "/operation/result" then
            fixture.complete = false
            response.terminal = true
            response.snapshot_required = true
            response.complete = false
        end
    end
    fixture:advance(4)
    assert(fixture.results[operation.id].outcome == "applied")
    assert(fixture:count("/operation/result") == 1 and fixture:count("/snapshot/start") == 2)
    assert(fixture.writes == 1 and fixture.complete)
end)

test("rapid Play/Edit interrupts a chunk response without any source attempt", function(fixture)
    fixture:start()
    local operation = fixture:operation("return 'chunked'", true)
    fixture.holdRoute = "/operation/chunk"
    fixture:advance(1)
    fixture:setMode(false)
    fixture:setMode(true)
    fixture:release()
    fixture:advance(2)
    local result = assert(fixture.results[operation.id])
    assert(fixture.updateCalls == 0 and fixture:count("/operation/chunk") == 1)
    assert(result.details.code == "session_changed" and result.details.write_attempted == false)
end)

test("path is rechecked inside UpdateSourceAsync", function(fixture)
    fixture:start()
    local operation = fixture:operation("return 'local'")
    fixture.beforeWrite = function(instance)
        instance.Name = "Renamed"
        instance.Changed:Fire("Name")
    end
    fixture:advance(1)
    assert(fixture.writes == 0 and fixture.results[operation.id].outcome == "rejected")
    assert(fixture.mirror[operation.path] == nil)
    assert(fixture.mirror["ReplicatedStorage/Renamed.luau"].source == "return 'base'")
end)

test("an identical concurrent source is confirmed without rewriting it", function(fixture)
    fixture:start()
    local operation = fixture:operation("return 'same'")
    fixture.beforeWrite = function(instance) fixture:setSource(instance, "return 'same'") end
    fixture:advance(1)
    assert(fixture.writes == 0 and fixture.results[operation.id].outcome == "applied")
end)

test("replacing the observer stops the old controller before new HTTP", function(fixture)
    fixture.holdRoute = "/snapshot/start"
    fixture:start()
    local replacement = newFixture()
    replacement.globals = fixture.globals
    replacement.beforeRequest = function()
        assert(fixture.connected == 0, "Old observer was active when replacement started")
    end
    replacement:start()
    fixture:release()
    assert(#fixture.requests == 1)
    replacement:close()
end)

test("server busy retries preserve the exact snapshot payload until acknowledged", function(fixture)
    local retries = 0
    fixture.busyResponse = function(endpoint)
        if endpoint == "/snapshot/start" and retries < 2 then
            retries += 1
            return { ok = true, retry = true, retry_after_seconds = 0.2 }
        end
    end
    fixture:start()
    assert(not fixture.complete and fixture:count("/snapshot/batch") == 0)
    fixture:advance(1)
    assert(fixture.complete and fixture:count("/snapshot/start") == 3)
    assert(fixture.requests[1].body == fixture.requests[2].body and fixture.requests[2].body == fixture.requests[3].body)
    assert(fixture.requests[3].payload.epoch == 0 and #fixture.warnings == 0)
end)

test("transport timeout retries the exact request without restarting inventory", function(fixture)
    fixture.holdRoute = "/snapshot/batch"
    fixture:start()
    assert(fixture.held and fixture:count("/snapshot/start") == 1 and fixture:count("/snapshot/batch") == 1)
    fixture:advance(9)
    assert(fixture.complete and fixture:count("/snapshot/start") == 1 and fixture:count("/snapshot/batch") == 2)
    assert(fixture.requests[2].body == fixture.requests[3].body)
    assert(fixture.requests[3].payload.epoch == 0 and #fixture.warnings == 0)
end)

test("server busy result retry never clears the pending result as an ACK", function(fixture)
    fixture:start()
    local operation = fixture:operation("return 'local'")
    local retries = 0
    fixture.busyResponse = function(endpoint)
        if endpoint == "/operation/result" and retries < 2 then
            retries += 1
            return { ok = true, retry = true, retry_after_seconds = 0.2, terminal = true, snapshot_required = true }
        end
    end
    fixture:advance(1)
    assert(fixture.results[operation.id] == nil and fixture.writes == 1)
    fixture:advance(1)
    assert(fixture.results[operation.id].outcome == "applied")
    assert(fixture:count("/operation/result") == 3 and fixture:count("/snapshot/start") == 1)
    assert(fixture.writes == 1 and #fixture.warnings == 0)
end)

test("mode changes interrupt a busy wait before retrying obsolete work", function(fixture)
    local intercepted = false
    fixture.busyResponse = function(endpoint)
        if endpoint == "/snapshot/start" and not intercepted then
            intercepted = true
            return { ok = true, retry = true, retry_after_seconds = 0.2 }
        end
    end
    fixture:start()
    fixture:setMode(false)
    fixture:setMode(true)
    fixture:advance(1)
    assert(fixture.requests[2].endpoint == "/snapshot/start" and fixture.requests[2].payload.epoch == 2)
    assert(fixture.requests[2].payload.snapshot_id ~= fixture.requests[1].payload.snapshot_id)
    assert(fixture.complete and #fixture.warnings == 0)
end)

test("stop during a busy wait prevents the next HTTP request", function(fixture)
    fixture.busyResponse = function()
        return { ok = true, retry = true, retry_after_seconds = 0.2 }
    end
    fixture:start()
    fixture.controller.stop()
    fixture:advance(1)
    assert(#fixture.requests == 1)
end)

test("server busy deadline is bounded without advancing the inventory", function(fixture)
    fixture.busyResponse = function()
        return { ok = true, retry = true, retry_after_seconds = 0.2 }
    end
    fixture:start()
    fixture:advance(55)
    assert(#fixture.warnings == 1 and string.find(fixture.warnings[1], "deadline", 1, true))
    assert(not fixture.complete and fixture:count("/snapshot/batch") == 0)

    for _, request in fixture.requests do
        assert(request.payload.epoch == 0 and request.time - fixture.requests[1].time <= 55)
        assert(request.body == fixture.requests[1].body)
    end
end)

test("snapshot finish survives a busy peer and one lost response", function(fixture)
    local began = fixture.now
    fixture.busyResponse = function(endpoint)
        if endpoint == "/snapshot/finish" and fixture.now - began < 35 then
            return { ok = true, retry = true, retry_after_seconds = 1 }
        end
    end
    fixture.holdRoute = "/snapshot/finish"
    fixture:start()
    fixture:advance(35)
    local stalledRequest = fixture.requests[#fixture.requests]
    assert(fixture.held and stalledRequest.timeout == 55)
    fixture:advance(56)
    local recoveredRequest

    for requestIndex = #fixture.requests, 1, -1 do
        if fixture.requests[requestIndex].endpoint == "/snapshot/finish" then
            recoveredRequest = fixture.requests[requestIndex]
            break
        end
    end

    assert(not fixture.held and fixture.complete and #fixture.warnings == 0)
    assert(recoveredRequest and recoveredRequest ~= stalledRequest)
    assert(recoveredRequest.payload.request_id == stalledRequest.payload.request_id)
    assert(recoveredRequest.body == stalledRequest.body)
    assert(recoveredRequest.time - began + recoveredRequest.timeout <= 120)
end)

test("moving a script to an excluded root removes its old mirror path", function(fixture)
    fixture:start()
    local excluded = fixture:folder("Excluded", fixture.game)
    fixture:move(fixture.script, excluded)
    fixture:advance(1)
    assert(fixture.mirror["ReplicatedStorage/Example.luau"] == nil)
    fixture:move(fixture.script, fixture.root)
    fixture:advance(1)
    assert(fixture.mirror["ReplicatedStorage/Example.luau"].source == "return 'base'")
end)

test("inventory detects ancestor moves out of observed roots", function(fixture)
    local folder = fixture:folder("Nested", fixture.root)
    fixture:move(fixture.script, folder)
    fixture:start()
    local path = "ReplicatedStorage/Nested/Example.luau"
    assert(fixture.mirror[path])
    local excluded = fixture:folder("Excluded", fixture.game)
    fixture:move(folder, excluded)
    fixture:advance(16)
    assert(fixture.mirror[path] == nil)
end)

test("snapshot ID mismatch rejects a stale server confirmation", function(fixture)
    local intercepted = false
    fixture.responseHook = function(endpoint, _, response)
        if endpoint == "/snapshot/finish" and not intercepted then
            intercepted = true
            response.snapshot_id = "obsolete-snapshot"
        end
    end
    fixture:start()
    fixture:advance(1)
    assert(fixture:count("/snapshot/start") == 2 and fixture.writes == 0)
end)

print("Observer lifecycle: " .. tostring(passed) .. " scenarios passed")
