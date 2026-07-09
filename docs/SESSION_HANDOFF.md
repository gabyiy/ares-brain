ARES Session Handoff

Last Updated: 2026-07-09

Current Version

ARES v1.47 - Internal Event Bus Skeleton

---

Current Status

The project has been fully reorganized into a modular architecture.

The original monolithic "intent_router.py" has been split into separate intent modules.

Phase 2 foundation work has started.

New foundation modules:

- Event Bus
- Memory v1 interface

Current Phase 2 wiring:

- Text REPL uses a shared event bus.
- Intent Router publishes the public Phase 2 events.
- Text REPL stores basic conversation turns through MemoryStore v1.

Phase 3 planning/foundation has started.

New Phase 3 skill modules:

- `skills.Skill`
- `skills.SkillContext`
- `skills.SkillResponse`
- `skills.SkillRegistry`
- `skills.SkillManager`
- `skills.SkillPlugin`
- `skills.builtin.TimeDateSkill`

The skill layer is minimally wired into the text REPL through `IntentRouter`.
Normal intents still run first; SkillManager is only a fallback when no intent matches.
Voice work has not started.

Phase 4 long-term memory recall has started.

New memory recall modules:

- `memory.UserProfileStore`
- `memory.detect_profile_facts`
- `skills.builtin.MemoryRecallSkill`

User profile facts are stored separately from conversation history.
The default profile path is `data/user_profile.json`, which is ignored by git to avoid committing personal facts.
The profile path can be overridden with `ARES_USER_PROFILE_PATH` for tests.

Automated tests have been added.

New test coverage:

- Event bus
- MemoryStore v1
- UserProfileStore
- MemoryRecallSkill
- SkillRegistry and SkillManager
- TimeDateSkill
- CalculatorSkill
- NotesStore
- NotesSkill
- TasksStore
- TasksSkill
- GoalsStore
- GoalsSkill
- ReminderScheduler
- ConversationContextManager
- IntentParser
- Planner
- MultiStepPlan
- Context-aware planner
- Action confirmation layer
- Adapter config and SecretsGuard
- RealWeatherAdapter skeleton
- RealWeatherAdapter HTTP logic
- RealMarketAdapter skeleton
- ExecutionPipeline
- ToolChain
- ToolAdapter
- WeatherSkill
- MarketSkill
- CalendarSkill
- DeviceAction framework
- DeviceActionSkill
- CoreService
- Confirmed Windows lock device action
- Confirmed Windows sleep device action
- Device app launcher skeleton
- Confirmed Windows app launcher
- Manual calculator launch verification script
- ToolSelector structured intent routing
- Text REPL profile recall flow

Pytest is configured to collect only `tests/`, because legacy interactive scripts under `scripts/` also use `test_*.py` names.

Phase 4 Tool Selection foundation has been added.

New tool selection module:

- `skills.ToolSelector`
- `skills.ToolSelection`

Selection behavior:

- Scores local skills instead of relying on first registered match only.
- Supports exact trigger matches, contained trigger phrases, token overlap, optional `selection_keywords`, optional `selection_priority`, and `run_before_intents` filtering.
- Currently routes `TimeDateSkill`, `MemoryRecallSkill`, `CalculatorSkill`, `GoalsSkill`, `NotesSkill`, `TasksSkill`, `WeatherSkill`, `MarketSkill`, `CalendarSkill`, and `DeviceActionSkill`.

Phase 4 CalculatorSkill has been added as the first real local tool.

New calculator module:

- `skills.builtin.CalculatorSkill`

Calculator behavior:

- Supports addition, subtraction, multiplication, division, parentheses, decimals, and bounded powers.
- Uses AST parsing with explicit operator handling; it does not use `eval()`.
- Rejects unsupported or unsafe input with a clear response.
- Runs as a priority skill so arithmetic questions are handled before generic knowledge lookup.

Phase 5 NotesSkill has been added as the first persistent local note system.

New notes modules:

- `memory.NotesStore`
- `memory.NoteRecord`
- `skills.builtin.NotesSkill`

Notes behavior:

- Stores notes in `data/notes.json`, which is ignored by git.
- Keeps notes separate from conversation memory and user profile memory.
- Supports `remember this...`, `save note...`, `take a note...`, `list my notes`, `show my notes`, `delete note <id>`, `delete all notes`, and `search notes <keyword>`.
- Requires explicit confirmation with `confirm delete all notes` before clearing all notes.
- Publishes `notes.recorded`, `notes.deleted`, and `notes.cleared`.
- Uses `ARES_NOTES_PATH` for test isolation.

Phase 6 TasksSkill has been added as the first offline reminders/tasks system.

New task modules:

- `memory.TasksStore`
- `memory.TaskRecord`
- `memory.ReminderScheduler`
- `skills.builtin.TasksSkill`

Tasks behavior:

- Stores tasks in `data/tasks.json`, which is ignored by git.
- Keeps tasks separate from conversation memory, user profile memory, and notes.
- Supports `add task...`, `remind me to...`, `list tasks`, `show tasks`, `mark task <id> done`, `delete task <id>`, and `clear completed tasks`.
- Each task stores id, text, created timestamp, optional due text, and completed state.
- Stores due text only; no notifications, calendar integration, voice, or GPT integration has been added.
- Publishes `tasks.recorded`, `tasks.completed`, `tasks.deleted`, and `tasks.completed_cleared`.
- Uses `ARES_TASKS_PATH` for test isolation.

ReminderScheduler foundation has been added.

Reminder scheduler behavior:

- Parses existing task due text without changing `data/tasks.json`.
- Supports `today`, `tomorrow`, `next week`, `in 10 minutes`, `in 2 hours`, and `at 18:00`.
- Provides `parse_due_text(text)`, `due_tasks(now)`, and `upcoming_tasks(now, limit)`.
- Returns incomplete due/upcoming tasks from `TasksStore`.
- Ignores invalid due text safely.
- Does not send notifications.
- Does not schedule background jobs.
- Does not call calendar APIs.
- Does not use voice, GPT, or external APIs.

Phase 7 ConversationContextManager has been added for short-term in-memory context.

New conversation context module:

- `core.ConversationContextManager`
- `core.ConversationTurn`
- `core.get_global_conversation_context`

Conversation context behavior:

- Keeps the last 20 handled skill turns in RAM.
- Each turn stores timestamp, user message, assistant response, and detected skill.
- Supports `last_message()`, `last_user_message()`, `last_assistant_message()`, `last_skill()`, `history(limit)`, and `clear()`.
- `SkillManager` records handled skill interactions automatically.
- `interfaces.text_repl` passes the shared global in-memory context to `SkillManager`.
- Does not save conversation context to disk.
- Does not use embeddings, GPT, external APIs, or voice.

Phase 8 structured IntentParser has been added.

New intent parser modules:

- `core.Intent`
- `core.IntentParser`

Intent behavior:

- `Intent` stores `intent_name`, `confidence`, `extracted_entities`, and `raw_text`.
- `IntentParser` recognizes `calculate`, `note`, `task`, `memory_recall`, `weather`, `market`, `calendar`, `time_date`, and `unknown`.
- Useful entities are extracted for local tools, including task action/text/due, note actions, calculator expressions, and memory recall topics.
- Example: `remember buy milk tomorrow` becomes a `task` intent with text `buy milk` and due text `tomorrow`.
- Example: `remember to buy milk` becomes a `task` intent with text `buy milk`.
- Example: `remember this idea: build ARES memory` becomes a `note` intent with note text `idea: build ARES memory`.
- Example: `calculate 15*8` becomes a `calculate` intent with expression `15*8`.
- Example: `show my notes` becomes a `note` intent.
- Example: `what is my birthday` becomes a `memory_recall` intent for the birthday profile fact.
- Example: `what did I tell you about my job` becomes a `memory_recall` intent with topic `my job`.
- The parser is deterministic and offline. It does not use AI, GPT, embeddings, voice, or external APIs.

Selection behavior:

- `SkillManager` parses user text into an `Intent` before calling `ToolSelector`.
- Skills can declare `intent_names` for structured matching.
- `ToolSelector` scores structured intent matches before legacy trigger scoring.
- Exact and contained trigger fallback paths remain available for unknown intents and compatibility.
- Loose token-overlap fallback is disabled for unknown structured intents so generic text does not get misrouted to local skills.
- `can_handle` fallback remains available for unknown intents and compatibility.
- `SkillContext.metadata` carries the parsed `intent` and extracted `entities`.
- `TasksSkill` consumes parser-derived task entities so `remember buy milk tomorrow` can route through the REPL as an offline task.

Multi-step Planner foundation has been added.

New planner modules:

- `core.PlanStep`
- `core.Plan`
- `core.MultiStepPlan`
- `core.Planner`

Planner behavior:

- Receives an `Intent`.
- Produces ordered executable steps.
- Returns a regular `Plan` for single-step requests.
- Returns a `MultiStepPlan` for compatible requests with more than one executable step.
- Supports goals, notes, tasks, calculator, weather, market, calendar, and conversation memory targets.
- Skips impossible steps and returns planning errors cleanly.
- Splits compatible compound requests such as `What's the weather tomorrow and remind me to go to the gym`.
- Splits compatible compound requests such as `Show my goals and today's calendar`.
- Reads `UserProfileStore`, `GoalsStore`, `NotesStore`, and `TasksStore` through existing safe interfaces when those stores are injected.
- Resolves context requests such as `remind me about my main goal tomorrow`, `what should I do next for my goals`, and `show my goals and notes about gym`.
- Returns deterministic empty-context responses when required local context is missing.
- Serializes plans and steps for tests, events, and REPL display.
- Planner never executes skills.
- Planner does not read data files directly.
- Planner does not write memory, goals, notes, or tasks.
- ToolSelector builds a plan before returning a skill selection.
- SkillManager executes multi-step local plans when needed.
- `interfaces.text_repl` supports `show plan` and `show steps`.
- No new skills, GPT, voice, notifications, calendar integration, external APIs, or storage format changes were added.

Execution Pipeline foundation has been added.

New execution modules:

- `core.ExecutionPipeline`
- `core.ExecutionResult`
- `core.StepResult`
- `core.RollbackHook`

Execution behavior:

- Receives a `Plan` from Planner.
- Executes each `PlanStep` sequentially.
- Calls registered local skills through SkillManager and SkillRegistry.
- Pauses before destructive or important actions and returns a confirmation request.
- Executes conversation memory steps through MemoryStore.
- Executes internal `planner_context` response steps for deterministic context-only answers.
- Stops safely on unrecoverable failures.
- Continues after recoverable local tool failures when appropriate.
- Aggregates all step outputs into one final response.
- Labels mixed successful and failed recoverable steps as `Partial results:`.
- Records start time, end time, duration, success/failure, returned data, and error message for every step.
- Publishes execution lifecycle events.
- Emits standard execution logs through `ares.execution`.
- Provides a no-op rollback hook interface for future reversible local actions.
- `interfaces.text_repl` supports `show execution` and `show last execution`.
- No new skills, GPT, voice, notifications, calendar integration, external APIs, or storage format changes were added.

Execution Pipeline verification hardening has been added.

New integration coverage:

- Live REPL multi-step plan creation.
- Live REPL notes plus calculator execution through ExecutionPipeline.
- Live REPL task plus memory execution through ExecutionPipeline.
- Live REPL recoverable partial failure reporting and continued execution.
- Live REPL `show execution` and `show last execution`.
- Live-path spy coverage for `SkillManager -> IntentParser -> Planner -> ExecutionPipeline -> Skill`.

Multi-step Planner hardening has been added.

New hardening coverage:

- Single-step requests still return regular `Plan` objects.
- Compatible multi-step requests return ordered `MultiStepPlan` objects.
- Weather plus reminder requests plan and execute through the live REPL path.
- Goals plus calendar requests plan and execute through the live REPL path.
- Three-step requests preserve planner and execution ordering.
- Recoverable step failures continue remaining work and report partial results.
- No GPT, internet access, real APIs, voice, notifications, or background automation were added.

Context-aware Planner foundation has been added.

New context-aware behavior:

- `SkillManager` injects its existing `UserProfileStore`, `GoalsStore`, `NotesStore`, and `TasksStore` handles into `ToolSelector`'s Planner.
- Planner reads context only through public store methods such as `get_favorite`, `list`, and `search`.
- Planner resolves main-goal reminders into task steps when goal context exists.
- Planner resolves favorite-profile reminders into task steps when profile context exists.
- Planner resolves notes-about requests into note search steps when matching notes exist.
- Planner answers next-goal questions from related open tasks, milestones, or deterministic fallback guidance.
- Missing context returns safe local `planner_context` responses instead of direct file access, GPT, or external calls.
- REPL integration verifies `what should I do next for my goals?` through the live path.
- No GPT, internet access, real APIs, voice, notifications, or background automation were added.

Action Confirmation Layer has been added.

New confirmation modules:

- `core.ConfirmationRequest`
- `core.ConfirmationDecision`
- `core.ConfirmationManager`

Confirmation behavior:

- `ExecutionPipeline` pauses before destructive or important `PlanStep` actions.
- One pending confirmation id is kept in memory for the active runtime.
- `SkillManager` handles `yes` and `confirm` as approval for the pending request.
- `SkillManager` handles `no` and `cancel` as cancellation.
- Missing pending confirmation returns a safe local failure message.
- Confirmed actions rerun with a confirmation-approved marker so they do not ask again.
- Multi-step plans stop safely at the confirmation step and do not run later steps until the user explicitly confirms the protected action.
- Protected actions include note deletion, delete-all notes, task deletion, clear-completed tasks, goal delete/pause/complete, and future external adapter write/delete actions.
- No GPT, internet access, real APIs, voice, notifications, or background automation were added.

Tool Chaining foundation has been added.

New chain modules:

- `core.ToolChain`
- `core.ToolChainResult`
- `core.ToolChainTraceStep`

ToolChain behavior:

- Receives a `Plan`.
- Enforces max chain depth 5.
- Rejects repeated step signatures to prevent loop-style chains.
- Records ordered execution trace.
- Keeps bounded chain history for REPL inspection.
- Delegates accepted plans to ExecutionPipeline.
- Supports memory plus calculator, note plus memory, and task/reminder plus memory examples.
- `interfaces.text_repl` supports `show chain` and `show chain history`.
- No external APIs, GPT, voice, weather, stocks, calendar, notifications, or storage format changes were added.

Long-Term Goal Management foundation has been added.

New goal modules:

- `memory.GoalsStore`
- `memory.GoalRecord`
- `skills.builtin.GoalsSkill`

Goals behavior:

- Stores goals in `data/goals.json`, which is ignored by git.
- Keeps goals separate from conversation memory, user profile memory, notes, and tasks.
- Goal fields include id, title, description, created timestamp, active/completed/paused status, priority, and milestones.
- Supports `add goal`, `list goals`, `show goal <id>`, `complete goal <id>`, `pause goal <id>`, `delete goal <id>`, and `add milestone to goal <id>`.
- `IntentParser`, `ToolSelector`, `Planner`, `ToolChain`, `ExecutionPipeline`, `SkillManager`, and the text REPL route the local `goal` intent.
- Tests cover GoalsStore persistence, GoalsSkill commands, ToolSelector routing, IntentParser routing, Planner path, ExecutionPipeline path, ToolChain goal chains, SkillManager path, REPL lifecycle commands, and persistence after reload.
- No GPT, autonomous background actions, notifications, external APIs, voice, weather, stocks, or calendar integration were added.

External Tool Adapter foundation has been added.

New adapter modules:

- `core.ExternalAdapterConfig`
- `core.SecretsGuard`
- `core.SecretValidationError`
- `core.ToolAdapter`
- `core.ToolRequest`
- `core.ToolResponse`
- `core.ToolAdapterRegistry`
- `core.MockWeatherAdapter`
- `core.RealWeatherAdapter`
- `core.MockMarketAdapter`
- `core.RealMarketAdapter`
- `core.MockCalendarAdapter`

ToolAdapter behavior:

- Validates future adapter config before execution.
- Tracks adapter enabled state, mode, API key environment variable name, base URL, and timeout.
- Rejects raw-looking secrets in adapter config payloads.
- Registers and looks up local adapters by name.
- Finds adapters by capability.
- Exposes adapter metadata: name, description, capabilities, `requires_network`, `requires_auth`, and `supports_real_mode`.
- Returns clear missing-adapter and unsupported-capability responses.
- Provides offline mock weather, market, and calendar adapters for tests only.
- Provides a real-weather-capable adapter that runs HTTP only after explicit real-mode config and env-key gates pass.
- Provides a real-market-capable adapter that runs HTTP only after explicit real-mode config and env-key gates pass.
- Planner accepts an optional ToolAdapterRegistry for future adapter-aware planning.
- ExecutionPipeline can execute explicit `tool_adapter` PlanSteps through an injected registry.
- No default real APIs, API keys, GPT, voice, calendar integration, web adapter, or ungated network calls were added.

Device Action Framework skeleton has been added.

New device action modules:

- `core.DeviceAction`
- `core.AppLaunchConfig`
- `core.AppAllowlistLoader`
- `core.AppAllowlistConfigError`
- `core.DeviceActionResult`
- `core.DeviceActionSafetyDecision`
- `core.DeviceActionConfirmationRequest`
- `core.DeviceActionRegistry`
- `core.LocalDeviceActionAdapter`

Device action behavior:

- Provides stable local action metadata and execution result models.
- Registers safe local actions by name.
- Returns safe failures for unknown actions.
- Exposes safe built-in actions: `echo`, `system_status_mock`, `list_actions`, and `list_apps`.
- Exposes confirmation-gated real actions: `lock_pc` and `sleep_pc`.
- Exposes confirmation-gated Windows app launcher action: `open_app`.
- Classifies device actions as `safe`, `confirmation_required`, or `forbidden`.
- Marks shutdown and restart as confirmation-required placeholders.
- Marks `lock_pc` as confirmation-required and implemented only after approval.
- Marks `sleep_pc` as confirmation-required and implemented only after approval.
- Marks `open_app` as confirmation-required and implemented only as an enabled allowlist Windows launcher after approval.
- Marks run command, delete, and arbitrary shell as forbidden placeholders.
- Returns stable confirmation-required or forbidden results without executing those actions.
- `system_status_mock` returns deterministic mock data and does not inspect the host.
- Future dangerous actions must require explicit confirmation before execution.
- No shutdown/restart, arbitrary app launching, arbitrary shell command execution, Telegram, voice, internet, GPT, remote control, notifications, or dangerous device automation was added.

DeviceActionSkill safe live path has been added.

New skill module:

- `skills.builtin.DeviceActionSkill`

Live path behavior:

- IntentParser recognizes `device_action` intents for safe device commands and unsafe device phrases.
- ToolSelector selects DeviceActionSkill through structured intent matching.
- Planner creates `device_action` plan steps.
- ExecutionPipeline executes the plan through the registered DeviceActionSkill.
- SkillManager carries `LocalDeviceActionAdapter` through SkillContext.
- Text REPL can execute safe device actions through the normal router flow.
- Supported commands are `echo <text>`, `list device actions`, `list apps`, `system status`, and confirmation-gated `lock pc`/`sleep pc`/`open app <app_id>`.
- Shutdown, restart, and unapproved `lock_pc`/`sleep_pc`/`open_app` return stable confirmation-required responses.
- Run command, delete, and arbitrary shell return stable forbidden responses.
- Confirmation-required device actions are never executed directly unless they are explicitly implemented and confirmed; currently `lock_pc`, `sleep_pc`, and allowlisted Windows `open_app` meet that rule.
- Forbidden device actions are never executed.
- No shutdown/restart, arbitrary app launching, arbitrary shell, Telegram, voice, internet, GPT, remote access, notifications, or background jobs were added.

Device dangerous-action confirmation gate has been added.

Danger gate behavior:

- Device action danger classification is centralized in `core.DeviceAction`.
- `DeviceActionSkill` uses the classification before calling `LocalDeviceActionAdapter`.
- Confirmation-required results include a stable `device-action-confirmation:<action>` token.
- Confirmation-required metadata records that the action was not executed.
- Forbidden metadata records that the action was not executed.
- Planner preserves the classification fields on `device_action` plan steps.
- The text REPL displays confirmation-required and forbidden responses safely through the normal assistant path.
- No real OS commands, shutdown/restart, arbitrary shell, Telegram, voice, GPT, internet, notifications, remote access, or background jobs were added.

Confirmed Windows lock device action has been added.

Lock behavior:

- `lock_pc` is the first real OS-backed device action.
- `lock_pc` requires explicit confirmation through `core.ConfirmationManager`.
- `ExecutionPipeline` pauses the `lock_pc` step before execution and stores a pending confirmation.
- `yes` or `confirm` reruns the step with `confirmation_approved`.
- `LocalDeviceActionAdapter` calls the Windows lock implementation only after confirmation approval.
- Non-Windows platforms return a safe unsupported response.
- Tests mock the Windows lock implementation; no test locks the workstation.
- Shutdown, restart, arbitrary app launching, run command, delete, arbitrary shell, Telegram, voice, GPT, internet, notifications, remote access, and background jobs were not added; mocked app launching arrived later in v1.30 and confirmed allowlisted Windows app launching arrived later in v1.31.

Confirmed Windows sleep device action has been added.

Sleep behavior:

- `sleep_pc` is the second real OS-backed device action.
- `sleep_pc` requires explicit confirmation through `core.ConfirmationManager`.
- `ExecutionPipeline` pauses the `sleep_pc` step before execution and stores a pending confirmation.
- `yes` or `confirm` reruns the step with `confirmation_approved`.
- `LocalDeviceActionAdapter` calls the Windows sleep implementation only after confirmation approval.
- Non-Windows platforms return a safe unsupported response.
- Tests mock the Windows sleep implementation; no test puts the workstation to sleep.
- Shutdown, restart, arbitrary app launching, run command, delete, arbitrary shell, Telegram, voice, GPT, internet, notifications, remote access, and background jobs were not added; mocked app launching arrived later in v1.30 and confirmed allowlisted Windows app launching arrived later in v1.31.

Device app launcher skeleton has been added.

App launcher behavior:

- `AppLaunchConfig` models app allowlist entries with `app_id`, `display_name`, `command_placeholder`, `enabled`, and `requires_confirmation`.
- `LocalDeviceActionAdapter` lists allowlisted apps with the safe `list_apps` action.
- `open_app <app_id>` requires explicit confirmation before execution.
- In this skeleton phase, confirmed `open_app` calls only the injected mocked launcher for enabled allowlisted apps.
- Unknown app ids and disabled app ids are rejected before the launcher callback is called.
- Command-like app ids are normalized and rejected if they are not allowlisted.
- IntentParser, Planner, ExecutionPipeline, SkillManager, DeviceActionSkill, and the text REPL all preserve the app id and confirmation gate.
- No real apps were launched in this skeleton phase, and no arbitrary shell commands, shutdown/restart/delete, Telegram, voice, GPT, internet, remote access, notifications, or background jobs were added.

Confirmed Windows app launcher has been added.

Confirmed app launcher behavior:

- `open_app <app_id>` still requires explicit confirmation through `core.ConfirmationManager`.
- Built-in allowlist examples originally started disabled by default: `notepad`, `calculator`, and `browser`.
- Confirmed `open_app` launches only enabled apps from the allowlist config.
- User input can only select an allowlisted `app_id`; user-provided paths and shell-like app ids are rejected.
- The Windows launcher uses the configured allowlist command with `shell=False`.
- Non-Windows platforms return a safe unsupported response.
- Tests mock the Windows launcher and do not open real apps.
- No shutdown, restart, delete, Telegram, voice, GPT, internet, remote access, notifications, arbitrary shell commands, or arbitrary app launching was added.

App launcher allowlist config has been added.

Allowlist config behavior:

- `config/apps.json` stores approved app definitions outside runtime code.
- The tracked examples originally started disabled by default: `notepad`, `calculator`, and `browser`.
- `AppAllowlistLoader` validates required `app_id`, `display_name`, command/path, `enabled`, and `requires_confirmation` fields.
- Invalid config, missing required fields, non-boolean flags, and duplicate normalized app ids fail safely before the adapter builds its runtime allowlist.
- `LocalDeviceActionAdapter` loads the config-backed allowlist by default and still accepts injected test allowlists for isolated tests.
- Confirmed `open_app` uses only the configured allowlist command; user-supplied command or path parameters are ignored and never become launch commands.
- Tests cover valid config loading, invalid config rejection, duplicate app id rejection, disabled and unknown app rejection, confirmed enabled launch through a mocked launcher, and user-supplied path isolation.
- No shutdown, restart, delete, Telegram, voice, GPT, internet, remote access, notifications, arbitrary shell commands, or arbitrary app launching was added.

Calculator app allowlist enablement has been added.

Calculator app behavior:

- `calculator` is the only enabled app in `config/apps.json`.
- `notepad` and `browser` remain disabled and fail safely before launch.
- `open_app calculator` still requires explicit confirmation through `core.ConfirmationManager`.
- Confirmed calculator launch uses only the existing safe Windows launcher path and configured allowlist command.
- User-supplied paths and shell-like app ids remain rejected.
- Unknown apps and disabled apps fail safely.
- Non-Windows platforms return unsupported safely.
- Tests mock the launcher and do not open Calculator during verification.
- No shutdown, restart, delete, Telegram, voice, GPT, internet, remote access, notifications, arbitrary shell commands, or arbitrary app launching was added.

Manual calculator launch verification has been added.

Manual verification behavior:

- New script: `scripts/manual_verify_calculator_launch.py`.
- The script prints a warning before any action.
- The script shows `App id: calculator`.
- It requires the exact typed confirmation `YES_OPEN_CALCULATOR`.
- It refuses every other input and does not build the device action adapter on refusal.
- After exact confirmation, it calls `LocalDeviceActionAdapter.execute("open_app", {"app_id": "calculator", "confirmation_approved": True})`.
- Tests inject a fake adapter and never open Calculator.
- No new apps, shutdown, restart, delete, shell commands, Telegram, voice, GPT, internet, remote access, or notifications were added.

PCService abstraction has been added.

PC service behavior:

- New module: `core/PCService.py`.
- `PCService` defines the dedicated interface for future PC operations: `lock()`, `sleep()`, `open_app(app_id)`, and `status()`.
- `PCServiceResult` defines the service-layer result shape.
- `PCStatus` defines the structured safe status object.
- `PCCapabilities` defines the structured safe capability object.
- `WindowsPCService` contains the Windows-specific implementation behind the service boundary.
- `LocalDeviceActionAdapter` delegates status, lock, sleep, and open-app behavior through PCService instead of directly calling Windows helpers.
- Existing lock, sleep, app launcher, confirmation, app allowlist, and test injection behavior remains compatible.
- Tests verify DeviceAction delegates status, lock, sleep, and open-app calls through PCService.
- No new device actions, shutdown, restart, delete, shell commands, Telegram, voice, GPT, internet, remote access, notifications, or behavior changes were added.

PCService status provider has been added.

Status provider behavior:

- `PCService.get_status()` returns structured safe status through `PCStatus`.
- Safe fields include operating system, hostname, current user, Python version, optional uptime, and available actions.
- `PCService.status()` remains a compatibility wrapper around `get_status()`.
- DeviceAction `system status` obtains status data only through PCService.
- Tests cover direct PCService status structure, the compatibility wrapper, DeviceAction status routing, and SkillManager pipeline status output.
- No network access, hardware telemetry, process enumeration, remote control, internet, GPT, voice, or new device actions were added.

PCService capability discovery has been added.

Capability discovery behavior:

- `PCService.get_capabilities()` returns structured safe capability data through `PCCapabilities`.
- Capability data includes supported device actions, supported applications, available status providers, available services, and explicit safeguards.
- DeviceAction `list device actions` and `list apps` obtain discovery data through PCService instead of direct hardcoded lists.
- Tests cover direct PCService capability structure, DeviceAction action discovery through PCService, and DeviceAction app discovery through PCService.
- No internet, GPT, network, remote execution, process enumeration, hardware telemetry, or new device actions were added.

CoreService orchestration layer has been added.

New orchestration modules:

- `core.CoreService`
- `core.CoreServiceResult`

CoreService behavior:

- Owns local/external service registration.
- Registers `PCService` as the `pc` service by default.
- Exposes `get_service(name)` for service lookup.
- Exposes `list_services()` for stable service metadata.
- Exposes `get_capabilities()` to aggregate capability data from every registered service.
- `SkillManager` carries the active CoreService for device action paths.
- `LocalDeviceActionAdapter` obtains PCService through CoreService where practical.
- Tests cover default PCService registration, service lookup, service listing, capability aggregation, capability failure reporting, DeviceAction discovery through CoreService, and SkillManager/CoreService handoff.
- No behavior changes, GPT, internet, network calls, remote execution, hardware additions, or new device actions were added.

Phase 2 architecture stabilization has been completed.

Phase 2 Complete architecture baseline:

- `PC_SERVICE_NAME` centralizes the default CoreService PC registration key.
- `SkillManager` carries the active `CoreService`.
- `SkillContext` now exposes that same `core_service` reference for consistent Brain-to-service access.
- `CoreService` remains the service registration, lookup, listing, and capability aggregation boundary.
- `PCService` remains the dedicated local PC operation/status/capability boundary.
- Services that participate in CoreService capability aggregation expose `get_capabilities()`.
- PC services expose `get_status()` and the compatibility `status()` wrapper.
- Missing service capability interfaces fail safely through CoreService capability aggregation.
- Tests cover default PCService status/capability interfaces, safe missing-capability reporting, and SkillManager/CoreService context propagation.
- No behavior changes, new functionality, new cities, GPT, internet, network calls, remote execution, hardware additions, or new device actions were added.

Permanent architecture reference has been created in `docs/ARCHITECTURE.md`.

Architecture reference contents:

- ARES Philosophy centered on the Brain as the stable identity layer.
- Capital City Architecture with Brain -> CoreService -> Registered Services -> Skills -> Adapters -> Devices.
- Current Services covering CoreService and PCService responsibilities.
- Device Action Pipeline from Brain to Skill to DeviceAction to PCService to Windows.
- Capability Discovery through `get_capabilities()`.
- Future City placeholders for Voice, Vision, Weather, Stocks, Codex, Home, and Robot cities.
- Upgrade Philosophy for replacing hardware, speech engines, AI models, cameras, and providers without changing the Brain.
- Design Rules for service boundaries, structured data, discovery, small modules, and tests before merge.
- Long-term Vision for ARES growing from Raspberry Pi into a humanoid robot without losing identity.
- This was documentation-only and did not add runtime behavior.

Voice City foundation has been added.

Voice City skeleton behavior:

- `core.VoiceService` defines the Voice City service interface.
- `core.PlaceholderVoiceService` provides safe placeholder status and capability responses.
- `core.VoiceStatus` reports that audio hardware access, microphone, speaker, background listening, internet, and GPT are disabled, and STT/TTS/wake word are not configured.
- `core.VoiceCapabilities` reports no supported voice actions, no input modes, no output modes, and explicit safeguards for microphone, speaker, STT, TTS, wake word, background listening, internet, and GPT.
- `CoreService` registers the placeholder VoiceService as `voice` by default alongside `pc`.
- CoreService capability aggregation now includes both PCService and VoiceService by default.
- Tests cover VoiceService registration through CoreService, capabilities, placeholder status, PC plus Voice capability aggregation, and no audio hardware access.
- No real microphone, speaker, Whisper, Vosk, Piper, STT, TTS, wake word, GPT, internet, or background listening was added.

Voice City STT/TTS contracts have been added.

Voice input/output contract behavior:

- `core.VoiceInput` defines `listen_once()`, `get_status()`, and `get_capabilities()`.
- `core.VoiceOutput` defines `speak(text)`, `get_status()`, and `get_capabilities()`.
- `core.NullVoiceInput` returns a safe placeholder listen result with no microphone access, no STT, no wake word, and no background listening.
- `core.NullVoiceOutput` accepts text as a safe placeholder and performs no speaker output or TTS.
- `PlaceholderVoiceService` owns the input/output components.
- VoiceService status includes input and output status blocks.
- VoiceService capabilities expose `voice_input: placeholder` and `voice_output: placeholder` plus component capability data.
- CoreService aggregation includes the updated VoiceService capability shape.
- Tests cover component ownership, NullVoiceInput placeholder behavior, NullVoiceOutput placeholder behavior, VoiceService status aggregation, CoreService capability aggregation, and no audio hardware access.
- No real microphone, speaker, Whisper, Vosk, Piper, STT, TTS, wake word, GPT, internet, or background listening was added.

Voice City text loop foundation has been added.

Voice loop behavior:

- `core.VoiceLoop` defines a one-shot Voice City text loop.
- `VoiceLoop.run_once()` calls `VoiceInput.listen_once()` once.
- Empty or missing input returns a safe no-input result and does not call the text handler.
- Recognized text is passed to an injected existing text/planner/execution handler.
- Final response text is passed to `VoiceOutput.speak(text)`.
- Default components remain `NullVoiceInput` and `NullVoiceOutput`.
- Safe failures are returned for input, handler, and output errors with explicit error messages.
- Tests cover empty input, recognized text reaching a mocked planner/execution handler, output text reaching `NullVoiceOutput`, and safe error paths.
- No microphone, speaker, wake word, background loop, real STT, real TTS, GPT, internet, new skills, or behavior changes outside Voice City were added.

Manual Voice City text simulation has been added.

Manual voice text simulation behavior:

- New script: `scripts/manual_voice_text_loop.py`.
- The script is owner-run and text-only.
- It prints a warning that no microphone or speaker will be used.
- It accepts one typed input line.
- It wraps that line in a safe typed-text `VoiceInput`.
- It passes the text through `VoiceLoop`.
- Non-empty text uses the existing local SkillManager planner/execution path.
- The final response is printed as text.
- Output uses `NullVoiceOutput`.
- Empty input exits safely.
- Tests cover import safety, mocked VoiceLoop handoff, real local calculator routing through the existing path, empty input behavior, and no audio hardware access.
- No microphone, speaker, wake word, background loop, real STT, real TTS, GPT, internet, real audio, or unconfirmed device action execution was added.

City lifecycle and lazy capability routing have been added.

City lifecycle behavior:

- CoreService tracks city states: `idle`, `active`, `failed`, and `disabled`.
- CoreService service metadata includes city status and registered capabilities.
- `CoreService.route_by_capability()` routes one request to one matching idle city.
- Only the selected city becomes active during the route.
- Unused cities remain idle and are not called.
- Disabled cities are skipped.
- A failed route marks only the selected city as `failed`.
- `CoreService.get_capabilities()` includes capability registry metadata and city status data.
- Event Bus city activation is documented as future-only. No event-driven city wakeup runtime was added.
- Tests prove unused cities are not called, disabled cities are not routed, and failed routes are reported safely.
- No real audio, GPT, internet, new APIs, external calls, notifications, or background activation was added.

Internal Core EventBus skeleton has been added.

Internal event bus behavior:

- New module: `core.EventBus`.
- `core.Event` stores source, type, priority, payload, and timestamp.
- Supported priorities are `low`, `normal`, `high`, and `critical`.
- `EventBus.publish()` creates an event and dispatches to subscribers for that event type.
- `EventBus.subscribe()` registers in-process handlers and returns an unsubscribe callback.
- Publishing without subscribers is safe.
- Event history is returned in priority order.
- This is future-use infrastructure for cities reporting important events without Brain polling.
- No real camera, notifications, background daemon, internet, GPT, new APIs, or event-driven city activation was added.

ARES Behavior Schematic has been documented in README and `docs/ARCHITECTURE.md`.

Behavior schematic summary:

- Capital = Brain identity.
- City Hall = CoreService.
- Cities = major services.
- Districts = sub-services.
- Villages = adapters.
- Houses = concrete devices, APIs, files, models.
- Brain stores identity-level knowledge only: long-term memory, short-term context, user profile, known people and friends, learned preferences, goals, personality, relationship history, and decision history.
- Replaceable services own implementation details, such as Weather City for weather APIs, Voice City for STT/TTS/wake word, Vision City for camera and face recognition, PC City for Windows/device actions, Codex City for GitHub/testing, and Home City for smart plugs and home devices.
- Examples document that the Brain can remember "Gabriel wants morning weather reports" while Weather City chooses the provider, and the Brain can remember "Andrei is a known friend" while Vision City handles face embeddings and matching.
- Design rule added: the Brain must never directly know API keys, Windows commands, camera internals, model internals, hardware commands, or provider-specific parsing.
- This was documentation-only and did not add GPT, internet, voice, camera, notifications, background listening, or runtime behavior.

External Adapter Config and SecretsGuard foundation has been added.

New config modules and files:

- `core.AdapterConfig`
- `core.ExternalAdapterConfig`
- `core.SecretsGuard`
- `core.SecretValidationError`
- `config/adapters.example.json`

Adapter config behavior:

- Config fields include `enabled`, `mode`, `api_key_env_name`, `base_url`, and `timeout_seconds`.
- Supported modes are `mock`, `local`, and `real`.
- `ToolAdapterRegistry` enforces config before adapter execution.
- Mock/local mode preserves existing offline WeatherSkill, MarketSkill, and CalendarSkill behavior.
- Real mode fails safely when an env key is missing, when the env-key name is only a placeholder, or when real execution is not implemented.
- Fake placeholders are allowed only as placeholders.
- Raw-looking API keys and tokens are rejected.
- Local/private adapter config files are ignored by git.
- No real API keys, real API calls, internet access, GPT, voice, notifications, or background automation were added.

Real Weather Adapter skeleton has been added.

New weather adapter module:

- `core.RealWeatherAdapter`

Real weather skeleton behavior:

- Uses explicit adapter name `real_weather`.
- Supports `weather.current` and `weather.forecast` capabilities.
- Marks `requires_network` and `requires_auth` as true.
- Is not registered in the default SkillManager adapter registry.
- Reads API keys only from the configured environment variable name.
- Fails safely when the env key is missing.
- Does not hardcode or store raw secrets.
- Does not expose raw env values in responses.
- `config/adapters.example.json` keeps `real_weather` disabled and mock-mode by default with fake placeholders.
- Existing WeatherSkill behavior stays on `mock_weather` unless a structured intent explicitly selects `real_weather`.
- No real weather API key, real weather API call, GPT, voice, calendar write, notification, or background job was added.

Real Weather Adapter HTTP logic has been added.

HTTP behavior:

- Runs only when adapter config mode is `real`.
- Requires the configured API key environment variable to exist before any HTTP call.
- Reads the API key value only from the environment variable.
- Does not store raw keys in config.
- Does not return raw env values in adapter responses.
- Passes configured timeout seconds to the HTTP client.
- Normalizes supported weather API payloads into ARES weather data with location, condition, temperature C, period, capability, and source.
- Returns safe deterministic errors for HTTP timeout, HTTP status errors, invalid JSON, and unrecognized weather payloads.
- Tests mock HTTP and make no real network calls.
- Default WeatherSkill behavior remains `mock_weather`.
- No real API key, default real mode, GPT, voice, calendar write, stocks real API, notification, or background job was added.

Real Market Adapter skeleton has been added.

New market adapter module:

- `core.RealMarketAdapter`

Real market skeleton behavior:

- Uses explicit adapter name `real_market`.
- Supports `market.quote` and `market.summary` capabilities.
- Marks `requires_network` and `requires_auth` as true.
- Is not registered in the default SkillManager adapter registry.
- Reads API keys only from the configured environment variable name.
- Fails safely when the env key is missing.
- Does not hardcode or store raw secrets.
- Does not expose raw env values in responses.
- `config/adapters.example.json` keeps `real_market` disabled and mock-mode by default with fake placeholders.
- Existing MarketSkill behavior stays on `mock_market` unless a structured intent explicitly selects `real_market`.
- No real market API key, default real mode, GPT, voice, calendar write, notification, or background job was added.

Real Market Adapter HTTP logic has been added.

HTTP behavior:

- Runs only when adapter config mode is `real`.
- Requires the configured API key environment variable to exist before any HTTP call.
- Reads the API key value only from the environment variable.
- Does not store raw keys in config.
- Does not return raw env values in adapter responses.
- Passes configured timeout seconds to the HTTP client.
- Normalizes supported market API payloads into ARES market data with symbol, price, currency, capability, source, and optional name/change fields.
- Returns safe deterministic errors for HTTP timeout, HTTP status errors, invalid JSON, and unrecognized market payloads.
- Tests mock HTTP and make no real network calls.
- Default MarketSkill behavior remains `mock_market`.
- No real API key, default real mode, GPT, voice, calendar write, notification, or background job was added.

Adapter-backed WeatherSkill has been added.

New weather module:

- `skills.builtin.WeatherSkill`

Weather behavior:

- Uses `ToolAdapterRegistry` and `MockWeatherAdapter`.
- Supports `weather`, `weather today`, `weather tomorrow`, and `weather in Madrid`.
- Runs as a priority local skill so weather requests are handled before legacy weather provider routing.
- `IntentParser`, `ToolSelector`, `Planner`, `ExecutionPipeline`, `SkillManager`, and the text REPL route the local `weather` intent.
- Tests cover weather intent parsing, mock weather adapter calls, WeatherSkill responses, planner weather steps, execution pipeline weather steps, REPL routing, and missing adapter errors.
- Does not call real APIs.
- Does not use API keys.
- Does not use internet access, GPT, voice, calendar, stocks, or notifications.

Adapter-backed MarketSkill has been added.

New market module:

- `skills.builtin.MarketSkill`

Market behavior:

- Uses `ToolAdapterRegistry` and `MockMarketAdapter`.
- Supports `stock nvidia`, `nvidia stock`, `apple stock`, and `market price for tesla`.
- Runs as a priority local skill so stock/market requests are handled before legacy stock provider routing.
- `IntentParser`, `ToolSelector`, `Planner`, `ExecutionPipeline`, `SkillManager`, and the text REPL route the local `market` intent.
- Tests cover market intent parsing, mock market adapter calls, MarketSkill responses, planner market steps, execution pipeline market steps, REPL routing, and missing adapter errors.
- Does not call real APIs.
- Does not use API keys.
- Does not use internet access, GPT, voice, calendar, or notifications.

Adapter-backed CalendarSkill has been added.

New calendar modules:

- `core.MockCalendarAdapter`
- `skills.builtin.CalendarSkill`

Calendar behavior:

- Uses `ToolAdapterRegistry` and `MockCalendarAdapter`.
- Supports `what is on my calendar today`, `calendar tomorrow`, `schedule today`, and `do I have anything tomorrow`.
- Runs as a priority local skill so calendar/schedule requests are handled before generic date or knowledge routing.
- `IntentParser`, `ToolSelector`, `Planner`, `ExecutionPipeline`, `SkillManager`, and the text REPL route the local `calendar` intent.
- Tests cover calendar intent parsing, mock calendar adapter calls, CalendarSkill responses, planner calendar steps, execution pipeline calendar steps, REPL routing, and missing adapter errors.
- Does not call Google Calendar.
- Does not call real APIs.
- Does not use API keys.
- Does not use internet access, GPT, voice, notifications, or background automation.

GitHub Actions CI has been added.

CI behavior:

- Workflow file: `.github/workflows/ci.yml`
- Runs on push to `main`.
- Runs on pull requests targeting `main`.
- Uses `windows-latest`.
- Sets up Python 3.13.
- Installs dependencies with `py -m pip install -r requirements.txt`.
- Runs `py -m pytest`.
- Runs `py -m compileall core interfaces events memory skills scripts`.
- Runs `py scripts\verify_phase2_events_memory.py`.
- Latest checked `main` CI run for `e37e5d4` completed successfully.
- `main` should be protected with required CI checks before merge.
- Future work should use: feature branch -> local tests -> pull request -> CI -> merge.

Strict engineering rules have been added in `docs/ENGINEERING_RULES.md`.

Required rules going forward:

- Never skip, xfail, or weaken failing tests without explicit approval.
- Never hide errors with broad try/except blocks.
- Fix root causes rather than symptoms.
- Keep the full verification suite passing before every push.
- Keep GitHub Actions CI green for `main` pushes and pull requests.
- Protect `main` with required CI checks and use pull requests for future changes.
- Update README and SESSION_HANDOFF after every meaningful change.

Master planning documents have been added.

New planning docs:

- `docs/ARCHITECTURE.md`
- `docs/ROADMAP.md`

These documents explain the current system flow, event bus, intent router, memory stores, skill system, REPL flow, future integration points, completed phases, current state, next priorities, blocked work, and testing rules before each phase.

Long-term city model architecture has been documented.

City model summary:

- ARES Brain is the capital city.
- The capital owns identity, memory, profile, goals, planning, decisions, and history with the owner.
- Specialized cities connect to the capital through explicit bridges.
- Planned specialized cities are Voice City, Vision City, Device/PC City, Weather City, Market City, Calendar City, Home City, Robot Body City, and Codex City.
- Core Services City provides scheduler, permissions, logging, configuration, health monitoring, plugin manager, secrets guard, and confirmation layer.
- Codex City is a future maintenance city that can check the ARES GitHub repository, pull latest code, run tests, check compile, check docs freshness, report problems, and suggest fixes.
- Codex City must never auto-edit without owner approval.

Future ARES home architecture vision has been documented.

Future architecture summary:

- Brain owns identity, memory, reasoning, planning, goals, personality, owner history, and decision history.
- CoreService is the future intent and capability router between the Brain and registered services.
- Cities are major ability modules.
- Adapters are hardware or API connectors owned by cities.
- Devices are physical bodies and interfaces.
- ARES Home Server is the future continuity point for the Brain, master memory, user profile, goals, relationship history, sync service, and backups.
- Raspberry Pi, Jetson Orin, Windows PC, phone, and future robot bodies act as replaceable clients or bodies.
- Hard rule: hardware-specific code must never enter the Brain.

Boundary:

- Documentation only.
- The home-server/client-body model is future vision, not implemented runtime behavior.
- No scheduler implementation was added.
- No GitHub API integration was added.
- No self-modifying behavior was added.
- No GPT, real voice/audio implementation, internet access, real APIs, or notifications were added.

Current working intents:

- Greeting
- Goodbye
- Weather
- News
- Knowledge
- Stocks

Current providers:

- Weather Provider
- News Provider
- Wikipedia Provider
- Knowledge Provider
- Alpha Vantage Stock Provider

The stock provider now uses Alpha Vantage instead of Yahoo Finance due to Yahoo's authentication restrictions.

Event Bus:

- New `events.EventBus` supports in-process publish/subscribe.
- Subscribers can listen to one event name or `"*"` for all events.
- `IntentRouter` now publishes `user_message_received`, `intent_detected`, and `response_generated`.
- Existing internal aliases remain: `input.received`, `intent.empty`, `intent.matched`, `intent.response`, and `intent.unmatched`.

Memory v1:

- New `memory.MemoryStore` reads the existing `data/memories_short.json` and `data/memories_long.json` files.
- New `memory.MemoryRecord` normalizes legacy memory entries without rewriting files until a write happens.
- Supports `remember`, `recall`, `promote`, `clear`, and `stats`.
- Publishes `memory.recorded`, `memory.promoted`, and `memory.cleared` events.

User Profile:

- Detects `My name is...`
- Detects `I live in...`
- Detects `My birthday is...`
- Detects `My favorite ... is...`
- Detects `I own...`
- Publishes `profile.fact_saved`

Notes Store:

- Stores note records with unique id, timestamp, and text.
- Lists, searches, deletes one note, and clears all notes through explicit skill confirmation.
- Uses `data/notes.json` by default.
- Does not write to `MemoryStore` or `UserProfileStore`.

Tasks Store:

- Stores task records with id, text, created timestamp, optional due text, and completed boolean.
- Lists, marks done, deletes one task, and clears completed tasks.
- Uses `data/tasks.json` by default.
- Does not write to `MemoryStore`, `UserProfileStore`, or `NotesStore`.
- ReminderScheduler can read task due text for passive due/upcoming queries.
- Does not send notifications or run background scheduling.

Goals Store:

- Stores goal records with id, title, description, created timestamp, status, priority, and milestones.
- Lists, shows, completes, pauses, deletes one goal, and adds milestones.
- Uses `data/goals.json` by default.
- Does not write to `MemoryStore`, `UserProfileStore`, `NotesStore`, or `TasksStore`.
- Does not run autonomous background actions, notifications, or external APIs.

Conversation Context:

- Stores recent handled skill turns in RAM only.
- Default limit is 20 turns.
- Is separate from `MemoryStore`, `UserProfileStore`, `GoalsStore`, `NotesStore`, and `TasksStore`.
- Is shared by the text REPL through `core.get_global_conversation_context()`.
- Does not write any conversation context file.

Text REPL memory:

- `interfaces.text_repl` creates one shared event bus and passes it to `IntentRouter`, `MemoryStore`, `UserProfileStore`, `GoalsStore`, `NotesStore`, `TasksStore`, `ConversationContextManager`, and `SkillManager`.
- Each text response records a short-term `conversation_turn` memory.
- Each text input is also scanned for user profile facts.
- Sleeping-mode responses also emit `user_message_received` and `response_generated`.
- The text REPL now also registers the built-in skill plugin.

Previous v0.7 modules compiled successfully.

Current Phase 2 foundation changes passed git whitespace checks locally.

---

Current Architecture

Intent Router
        │
        ├── EventBus lifecycle events
        │
        ├── GreetingIntent
        ├── GoodbyeIntent
        ├── WeatherIntent
        ├── NewsIntent
        ├── KnowledgeIntent
        └── StockIntent

Each intent owns its own logic and communicates with its corresponding provider.

MemoryStore v1 is separate from the legacy `memory_manager.py` API so existing scripts keep working.

The built-in skill plugin currently registers `MemoryRecallSkill`, `CalculatorSkill`, `CalendarSkill`, `DeviceActionSkill`, `GoalsSkill`, `MarketSkill`, `NotesSkill`, `TasksSkill`, `WeatherSkill`, and `TimeDateSkill`.
The REPL priority skill path currently covers profile memory recall, calculator arithmetic, goal commands, note commands, task commands, weather commands, stock/market commands, calendar/schedule commands, and safe device action commands.
`SkillManager` parses text into `core.Intent` before `ToolSelector` selects a local skill.
`ToolSelector` builds a `core.Plan` or `core.MultiStepPlan` before selection, and its Planner can use safe injected store interfaces for local context.
`SkillManager` owns a `CoreService` for local/external service registration and capability aggregation where practical.
`SkillContext` carries the same `core_service` reference used by SkillManager.
`SkillManager` handles confirmation decisions through `core.ConfirmationManager` before normal intent parsing.
`SkillManager` validates executable plan steps through `core.ToolChain`, and accepted chains execute through `core.ExecutionPipeline`.
`ExecutionPipeline` can execute weather skill PlanSteps, market skill PlanSteps, calendar skill PlanSteps, device action PlanSteps, and explicit `tool_adapter` PlanSteps through `core.ToolAdapterRegistry`.
`ExecutionPipeline` can execute internal `planner_context` PlanSteps for context-only responses.
`ExecutionPipeline` pauses destructive actions with `ConfirmationRequest` instead of executing them immediately.
`ExecutionPipeline` aggregates all step outputs and reports mixed recoverable success/failure as partial results.
`SkillManager` records handled skill turns in `ConversationContextManager`.

Skill Manager
        │
        ├── SkillRegistry
        ├── ToolSelector
        ├── SkillPlugin
        └── Built-in skills
            ├── MemoryRecallSkill
            └── TimeDateSkill

Text REPL
        │
        ├── IntentRouter
        ├── SkillManager fallback
        ├── Priority memory recall skill
        ├── EventBus
        ├── MemoryStore v1
        └── UserProfileStore

---

Immediate Next Milestone

Voice wake word/STT/TTS planning on top of the safe Voice City contracts and one-shot text loop. Do not add real audio hardware access without explicit approval.

Next technical choices:

- Add profile acknowledgement responses if desired; current fact statements are stored even when the response is generic.
- Keep voice, GPT, embeddings, external weather/stocks/calendar APIs, real scheduling, notifications, and Raspberry Pi deployment out of scope until explicitly approved.
- Connect selected daily reflection scripts and future providers to MemoryStore v1 only after the memory contract is documented.

---

Future Roadmap

1. GPT fallback integration
2. Voice interface
3. Raspberry Pi deployment
4. Robot body / sensors
5. Vision
6. Robotics
7. Jetson Orin migration
8. Autonomous ARES

Verification Notes

- `scripts/verify_phase2_events_memory.py` verifies router event publication and memory turn storage with temporary memory files.
- Run it with `python scripts/verify_phase2_events_memory.py`.
- Automated tests run with `py -m pytest`.
- Current pytest collection: 325 tests.
- Phase 3 skill package compiles with `py -m compileall skills`.
- `SkillManager` was manually checked with the built-in time/date skill.
- Text REPL was verified with `hello`, `what time is it`, `what date is it`, and `quit`.
- Long-term profile recall was verified through the text REPL with name, location, birthday, favorite tank, and owned item facts.
- Current verification passed:
  - `py -m pytest`
  - `py -m compileall core interfaces events memory skills scripts`
  - `py scripts\verify_phase2_events_memory.py`
- GitHub Actions CI runs the same verification suite on Windows with Python 3.13 for `main` pushes and pull requests.
- GitHub Actions should be checked after push for the latest `main` commit.
- Tool selection tests cover current TimeDate/MemoryRecall/Calculator/Goals/Notes/Tasks selection.
- Calculator tests cover simple arithmetic, precedence, parentheses, decimals, bounded powers, unsafe input rejection, and the REPL routing path.
- Notes tests cover add, list, search, delete, duplicate note text, empty note rejection, persistence after reload, ToolSelector routing, and the REPL routing path.
- Tasks tests cover add, list, mark done, delete, empty task rejection, persistence after reload, ToolSelector routing, and the REPL routing path.
- Goals tests cover add, list, show, complete, pause, delete, add milestone, persistence after reload, ToolSelector routing, IntentParser routing, Planner path, ExecutionPipeline path, ToolChain goal chains, SkillManager path, REPL lifecycle commands, and the REPL routing path.
- ToolAdapter tests cover adapter registration, lookup, missing adapter responses, mock weather responses, mock market responses, no-network/no-auth metadata, Planner registry wiring, and ExecutionPipeline adapter execution.
- CoreService tests cover service registration, lifecycle metadata, capability registry metadata, lazy route-by-capability behavior, unused city idle behavior, disabled city routing prevention, and failed route state handling.
- Core EventBus tests cover event dataclass normalization, publish, subscribe, unsubscribe, no-subscriber safety, priority ordering, invalid priority rejection, and stable priority levels.
- VoiceService tests cover CoreService registration, safe placeholder capabilities, safe placeholder status, VoiceInput/VoiceOutput ownership, NullVoiceInput listen placeholders, NullVoiceOutput speak placeholders, CoreService aggregation of PCService and VoiceService, VoiceLoop defaults, no-input behavior, recognized text routing to a mocked planner/execution handler, response handoff to NullVoiceOutput, safe input/handler/output failures, and no audio hardware access.
- DeviceAction tests cover registry registration/listing, app allowlist config loading, calculator enabled state, invalid config rejection, duplicate app id rejection, unknown action safe failure, echo, list actions, list apps, structured PCService status, structured PCService capability discovery, CoreService-backed service registration/capability aggregation, default PCService status/capability interfaces, safe missing-capability reporting, stable result formatting, PCService delegation for status/lock/sleep/open-app calls, CoreService-backed action/app discovery, danger classification, confirmation-required placeholders, forbidden placeholders, unapproved `lock_pc`/`sleep_pc`/`open_app`, confirmed mocked Windows lock/sleep, confirmed Windows calculator launch through a mocked launcher, unknown/disabled app rejection, notepad/browser disabled handling, arbitrary path rejection, shell-like input rejection, user-supplied path isolation, non-Windows unsupported handling, shutdown/restart remaining non-executable, and not-executed dangerous results.
- Manual calculator launch verification tests cover refusal without exact confirmation, the exact open_app device action path with mocked adapter, and safe adapter failure reporting without opening Calculator.
- Manual Voice City text simulation tests cover import safety, typed text reaching VoiceLoop, real local calculator routing through the existing SkillManager planner/execution path, empty input safe exit, and no audio hardware access.
- DeviceActionSkill tests cover echo, list actions, list apps, structured system status, shutdown/restart confirmation-required responses, unapproved `lock_pc`/`sleep_pc`/`open_app`, confirmed `lock_pc`/`sleep_pc`/`open_app` through SkillManager, run command/delete forbidden responses, unknown action safe failure, ToolSelector routing, Planner routing, SkillManager/CoreService handoff, SkillContext CoreService propagation, SkillManager/ExecutionPipeline confirmation-required handling, and text REPL display.
- Adapter config guard tests cover mock mode, real-mode missing-env failure, placeholder handling, raw-secret rejection, example config loading, read-only mock adapter behavior, and confirmation-layer compatibility.
- RealWeatherAdapter tests cover default mock weather behavior, real adapter instantiation, real-mode missing-env failure, mocked HTTP success, timeout safe errors, bad API response safe errors, HTTP status safe errors, normalized output stability, env-key-name-only config, raw env value non-exposure, safe WeatherSkill adapter failure handling, raw-looking key rejection, and SecretsGuard example-config compatibility.
- RealMarketAdapter tests cover default mock market behavior, real adapter instantiation, real-mode missing-env failure, mocked HTTP success, timeout safe errors, bad API response safe errors, HTTP status safe errors, normalized output stability, env-key-name-only config, raw env value non-exposure, safe MarketSkill adapter failure handling, raw-looking key rejection, and SecretsGuard example-config compatibility.
- WeatherSkill tests cover weather intent parsing, mock adapter calls, WeatherSkill responses, ToolSelector routing, planner weather steps, execution pipeline weather steps, REPL routing, full live path into `MockWeatherAdapter`, ToolChain loop prevention for repeated weather steps, and missing adapter errors.
- MarketSkill tests cover market intent parsing, mock adapter calls, MarketSkill responses, ToolSelector routing, planner market steps, execution pipeline market steps, REPL routing, and missing adapter errors.
- CalendarSkill tests cover calendar intent parsing, mock adapter calls, CalendarSkill responses, ToolSelector routing, planner calendar steps, execution pipeline calendar steps, REPL routing, and missing adapter errors.
- ReminderScheduler tests cover tomorrow parsing, relative minutes/hours, clock time parsing, due task detection, upcoming task ordering, and invalid due text handling.
- Planner tests cover single-step plans, two-step plans, mixed notes/calculator, mixed task/memory, goal steps, invalid plans, ordering, serialization, ToolSelector plan attachment, SkillManager execution, and REPL plan display.
- Multi-step planner tests cover single-step compatibility, weather plus reminder, goals plus calendar, three-step plans, planner ordering, execution ordering, partial failure recovery, and REPL integration.
- Context-aware planner tests cover goal context, profile favorite context, notes context, task context, missing context, multi-step context plans, partial failure recovery, and REPL integration.
- Confirmation tests cover delete note/task/goal confirmation, confirm execution, cancel behavior, missing pending confirmation, unaffected weather/market/calendar paths, future external write confirmation, clear completed tasks, goal status changes, and multi-step confirmation pause behavior.
- ExecutionPipeline tests cover single-step execution, multi-step execution, notes plus calculator, task plus memory, unrecoverable failure, recoverable partial failure, execution ordering, execution logging, rollback hooks, SkillManager integration, REPL execution display, live REPL multi-step planning, live REPL pipeline execution, live REPL partial failure reporting, and live-path component usage.
- ToolChain tests cover memory plus calculator, note plus memory, task/reminder plus memory, ordering, max depth enforcement, repeated-step loop prevention, and REPL chain display/history.
- Conversation context tests cover history ordering, max history size, clear, retrieval APIs, SkillManager integration, and REPL integration.
- Intent parser tests cover intent detection, confidence values, entity extraction, goal commands, ambiguous local phrasing, unknown intent, ToolSelector integration, SkillManager integration, live REPL parser use, and the REPL task path.
- `git diff --check` passed after the automated test changes.
- Runtime Python checks may not be available in some Windows sessions if `python`/`py` are not installed, only Microsoft Store aliases are present.
- Config and logging were left unchanged because the event bus and memory v1 work did not require changes there.

Latest Commits

- `032f132` Add internal core event bus skeleton
- `16cc8d4` Add city lifecycle lazy routing
- `9ac2de7` Add manual Voice City text loop simulation
- `b2b7a48` Add Voice City text loop foundation
- `fa98b30` Add Voice City input output contracts
- `6245ef4` Add Voice City service skeleton
- `432a70a` Create permanent architecture reference
- `5726813` Document Phase 2 architecture completion
- `8049fab` Stabilize core service architecture
- `ade5197` Document core service orchestration layer
- `78ced08` Add core service orchestration layer
- `9740e6d` Document PC service capability discovery
- `aa281b7` Add PC service capability discovery
- `44f7289` Document PC service status provider
- `b4af64d` Add structured PC service status provider
- `ab9ea2f` Add PC service abstraction for device actions
- `0af1e5c` Add manual calculator launch verification script
- `090043e` Enable calculator app allowlist entry
- `3486663` Add config-backed app allowlist loader
- `d15a528` Add confirmed Windows app launcher
- `8b6e7fc` Add safe device app launcher skeleton
- `6bab5a2` Add confirmed Windows sleep device action
- `65660aa` Add confirmed Windows lock device action
- `5a789bf` Add device action danger confirmation gate
- `63e4a6e` Add device action skill live path
- `694da9e` Add safe device action framework skeleton
- `357f984` Implement gated real market HTTP adapter
- `88e739e` Add real market adapter skeleton
- `d95ace8` Implement gated real weather HTTP adapter
- `4fbb734` Add real weather adapter skeleton
- `40ea8e5` Add external adapter config guard
- `fbf4492` Add action confirmation layer
- `79f7e4a` Add context-aware planner support
- `7683dbc` Harden multi-step planner execution
- `8678a16` Add external tool adapter foundation
- `f7f5e78` Add adapter-backed local weather skill
- `7d56178` Harden weather live path tests
- `7e7f3e3` Add adapter-backed local market skill
- `63027a6` Add adapter-backed local calendar skill
- `bc5a4e7` Deepen goals live path integration tests
- `4e5ec44` Add local long-term goal management
- `5bad0d5` Add local tool chaining foundation
- `b742116` Add REPL integration tests for execution pipeline
- `cee3841` Add execution pipeline for planner steps
- `98d8ff0` Document multi-step planner foundation
- `aaca6b4` Add local multi-step planner foundation
- `5cf4a3e` Document reminder scheduler foundation
- `0ba1c90` Add reminder scheduler foundation
- `c7b665d` Document intent parser runtime integration
- `8ae29d7` Deepen intent parser runtime integration tests
- `85f2a6c` Update docs for parser hardening
- `34a7b57` Harden intent parser phrase handling
- `e3c8501` Update docs for intent parser
- `f2e7a6b` Add structured intent parser

Next Planned Step

- Plan Voice wake word/STT/TTS integration only after explicit approval.
- Keep CI green before merging or pushing further changes.
- Prefer feature branch -> local verification -> PR -> CI -> merge for future work.
- Do not enable default real weather/market API behavior, Google Calendar integration, GPT, embeddings, real voice/audio hardware, vision, scheduling, notifications, or background automation yet.
- Do not start microphone access, speaker output, wake word detection, real STT, real TTS, or background listening yet.
