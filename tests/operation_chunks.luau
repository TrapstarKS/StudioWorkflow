local fs = require("@lune/fs")
local process = require("@lune/process")
local luau = require("@lune/luau")

local observer = fs.readFile(process.args[1])
luau.compile(observer)

local startPosition = assert(string.find(observer, "local function sendPendingResults", 1, true))
local endPosition = assert(string.find(observer, "\ntable.insert(connections", startPosition, true))
local applySource = string.sub(observer, startPosition, endPosition - 1)

local prelude = [[
local serde = require("@lune/serde")
local fs = require("@lune/fs")
local process = require("@lune/process")
local fixture = serde.decode("json", fs.readFile(process.args[2]))
local running = true
local editing = true
local initialized = true
local current = fixture.baseline
local instance = {}
local instancesById = { [fixture.operation.instance_id] = instance }
local identities = { [instance] = fixture.operation.instance_id }
local dirty = {}
local epoch = 0
local outboundPaused = false
local pendingResults = {}
local operationResults = {}
local attemptedOperations = {}
local incompleteSnapshot = {}
local pausedWork = {}
local writes = 0
local fetched = 0
local outcome = nil
local scenario = "success"
local HttpService = {}
local ScriptEditorService = {}

function HttpService:JSONDecode(text)
    return serde.decode("json", text)
end

local function canEdit()
    return editing
end

local function checkEpoch(expectedEpoch, requireEdit)
    assert(running and expectedEpoch == epoch and (not requireEdit or editing), "Obsolete operation")
end

local function markDirty(target)
    dirty[target] = true
end

local function supported()
    return true
end

local function paths()
    return fixture.operation.path
end

local function flush()
end

local function post(endpoint, payload, expectedEpoch, requireEdit)
    checkEpoch(expectedEpoch, requireEdit)

    if endpoint == "/operation/result" then
        outcome = payload.outcome
        return {}
    end

    fetched += 1

    if scenario == "missing" and fetched == 2 then
        error("HTTP failure")
    end

    local chunk = table.clone(fixture.chunks[payload.position])

    if scenario == "wrong_chunk" then
        chunk.position += 1
    end

    if scenario == "play" and fetched == 1 then
        editing = false
    end

    checkEpoch(expectedEpoch, requireEdit)
    return chunk
end

function ScriptEditorService:UpdateSourceAsync(target, callback)
    assert(fetched == fixture.operation.chunks)

    if scenario == "concurrent" then
        current = "another user's edit"
    elseif scenario == "callback_play" then
        editing = false
    end

    local result = callback(current)

    if result then
        writes += 1
        current = result
    end
end

function ScriptEditorService:GetEditorSource()
    return current
end
]]

local checks = [[
for _, testCase in { "success", "missing", "wrong_chunk", "play", "concurrent", "callback_play" } do
    scenario = testCase
    current = fixture.baseline
    editing = true
    writes = 0
    fetched = 0
    outcome = nil
    pendingResults = {}
    operationResults = {}
    attemptedOperations = {}

    local success = pcall(applyOperation, table.clone(fixture.operation), epoch)

    if testCase == "success" then
        assert(success and writes == 1 and outcome == "applied")
        assert(current == fixture.source)
    elseif testCase == "concurrent" then
        assert(success and writes == 0 and outcome == "rejected")
        assert(current == "another user's edit")
    else
        assert(not success and writes == 0 and outcome == nil)
        assert(current == fixture.baseline)
    end
end
]]

luau.load(prelude .. applySource .. checks)()
