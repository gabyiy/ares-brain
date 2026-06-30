ARES Architecture

This document describes the current ARES architecture and the intended integration points for future work. It is a planning document only; it does not introduce new runtime behavior.

Current System Flow

1. The user starts the text REPL with `py interfaces\text_repl.py`.
2. The REPL creates one shared `EventBus`.
3. The REPL creates `MemoryStore` for conversation turns.
4. The REPL creates `UserProfileStore` for persistent user facts.
5. The REPL creates `NotesStore` for persistent local notes.
6. The REPL creates `TasksStore` for persistent offline tasks.
7. The REPL creates `SkillManager`, registers the built-in skill plugin, and passes the manager to `IntentRouter`.
8. User input is sent to `IntentRouter`.
9. `IntentRouter` publishes input lifecycle events.
10. Priority skills are selected by `ToolSelector` before normal intents only when a skill opts in.
11. Normal intents run next.
12. Non-priority skills are selected by `ToolSelector` as a fallback when no normal intent matches.
13. Responses are published as events.
14. The REPL stores each conversation turn in `MemoryStore`.
15. The REPL scans each user message for profile facts and stores them in `UserProfileStore`.

Event Bus

`events.EventBus` is the in-process publish/subscribe layer.

Current responsibilities:

- Publish user input lifecycle events.
- Publish intent and skill detection events.
- Publish response generation events.
- Publish memory and profile write events.
- Keep a bounded event history for verification and tests.

Current event examples:

- `user_message_received`
- `intent_detected`
- `response_generated`
- `memory.recorded`
- `memory.promoted`
- `memory.cleared`
- `profile.fact_saved`
- `notes.recorded`
- `notes.deleted`
- `notes.cleared`
- `tasks.recorded`
- `tasks.completed`
- `tasks.deleted`
- `tasks.completed_cleared`
- `skill.registered`
- `skill.plugin_registered`
- `skill.detected`
- `skill.response_generated`

Intent Router

`core.intent_router.IntentRouter` remains the main text routing path.

Current order:

1. Empty input handling.
2. Priority skill fallback path for skills that must run before generic intents.
3. Normal intent modules.
4. Non-priority skill fallback path.
5. Unknown response.

Current intent modules:

- Greeting
- Goodbye
- Weather
- News
- Knowledge
- Stocks

MemoryStore

`memory.MemoryStore` is the v1 structured memory interface for conversation-style memories.

Current responsibilities:

- Store short-term memories.
- Store long-term memories.
- Recall memories by category, tags, text query, and importance.
- Promote a short-term memory to long-term.
- Clear memory files when explicitly requested.

Current storage:

- `data/memories_short.json`
- `data/memories_long.json`

UserProfileStore

`memory.UserProfileStore` stores user facts separately from conversation history.

Current responsibilities:

- Detect profile facts from user text.
- Store profile facts persistently.
- Recall profile values for personal memory questions.

Current supported fact patterns:

- `My name is...`
- `I live in...`
- `My birthday is...`
- `My favorite ... is...`
- `I own...`

Current storage:

- `data/user_profile.json`

The profile file is ignored by git because it can contain personal facts. Tests can override the path with `ARES_USER_PROFILE_PATH`.

NotesStore

`memory.NotesStore` stores user-created notes separately from conversation history and user profile facts.

Current responsibilities:

- Add notes with a unique id, timestamp, and text.
- List all notes.
- Search notes by keyword.
- Delete one note by id.
- Clear all notes only through an explicit confirmation flow in `NotesSkill`.

Current storage:

- `data/notes.json`

The notes file is ignored by git because it can contain personal notes. Tests can override the path with `ARES_NOTES_PATH`.

TasksStore

`memory.TasksStore` stores offline tasks and simple reminders separately from conversation history, user profile facts, and notes.

Current responsibilities:

- Add tasks with an id, text, created timestamp, optional due text, and completed flag.
- List all tasks.
- Mark one task completed by id.
- Delete one task by id.
- Clear completed tasks.

Current storage:

- `data/tasks.json`

The tasks file is ignored by git because it can contain personal tasks. Tests can override the path with `ARES_TASKS_PATH`.

Scheduling is not active. The current task system stores due text only; it does not schedule jobs, send notifications, call calendar APIs, or use an LLM.

SkillRegistry

`skills.SkillRegistry` owns skill registration and lookup.

Current responsibilities:

- Register skills.
- Reject duplicate skill names.
- Return all skills.
- Find matching skills for text input.
- Filter priority skills with `run_before_intents`.

ToolSelector

`skills.ToolSelector` chooses the best local skill for a user text request.

Current scoring rules:

- Exact trigger match gets the strongest confidence.
- Contained trigger phrase gets high confidence.
- Trigger token overlap gets partial confidence.
- Skills can add `selection_keywords` without changing selector code.
- Skills can add `selection_priority` for explicit tie-breaking.
- Selection can be filtered with `run_before_intents`.

Current supported runtime skills:

- `TimeDateSkill`
- `MemoryRecallSkill`
- `CalculatorSkill`
- `NotesSkill`
- `TasksSkill`

Future local skills should define clear triggers and optional `selection_keywords` so they can use the same selector without a giant if/else chain.

SkillManager

`skills.SkillManager` owns skill detection and execution.

Current responsibilities:

- Register individual skills.
- Register skill plugins.
- Select the best matching local skill through `ToolSelector`.
- Execute a skill with `SkillContext`.
- Publish skill lifecycle events.

Skill context currently carries:

- `event_bus`
- `memory_store`
- `profile_store`
- `notes_store`
- `tasks_store`
- `metadata`

Built-In Skills

Current built-in plugin:

- `skills.builtin.create_builtin_plugin`

Current built-in skills:

- `TimeDateSkill`
- `MemoryRecallSkill`
- `CalculatorSkill`
- `NotesSkill`
- `TasksSkill`

`TimeDateSkill` answers local time and date questions.

`MemoryRecallSkill` answers profile questions from `UserProfileStore` without using an LLM. It is a priority skill so questions such as `What is my name?` are answered before the generic knowledge intent.

`CalculatorSkill` answers local arithmetic questions without using an LLM. It supports addition, subtraction, multiplication, division, parentheses, decimals, and bounded powers through AST parsing and explicit operator handling, not `eval()`. It rejects unsupported or unsafe input with a clear response.

`NotesSkill` stores, lists, searches, and deletes local notes through `NotesStore`. It supports `remember this...`, `save note...`, `take a note...`, `list my notes`, `show my notes`, `delete note <id>`, `delete all notes`, and `search notes <keyword>`. `delete all notes` requires explicit confirmation with `confirm delete all notes`.

`TasksSkill` stores and manages offline reminders/tasks through `TasksStore`. It supports `add task...`, `remind me to...`, `list tasks`, `show tasks`, `mark task <id> done`, `delete task <id>`, and `clear completed tasks`. It stores optional due text but does not schedule or notify.

No real scheduling, notifications, voice, weather, stocks, calendar, external API, or GPT integration has been added as part of the tasks milestone.

REPL Flow

`interfaces.text_repl` is the active user interface.

Current responsibilities:

- Wake on `hello`, `hello ares`, `hi ares`, or `hey ares`.
- Exit on `goodbye`, `goodbye ares`, `exit`, or `quit`.
- Share one event bus across router, memory, profile, notes, tasks, and skills.
- Store each user/ARES turn as a conversation memory.
- Scan each user message for profile facts.
- Route note commands to `NotesSkill` and persist notes in `NotesStore`.
- Route task commands to `TasksSkill` and persist tasks in `TasksStore`.
- Print the final ARES response.

Future Integration Points

Voice

Voice should connect at the interface layer, beside the text REPL. It should reuse:

- `EventBus`
- `IntentRouter`
- `MemoryStore`
- `UserProfileStore`
- `NotesStore`
- `TasksStore`
- `SkillManager`

Voice must not bypass the existing routing, memory, or verification rules.

Vision

Vision should enter as a separate interface or provider layer. It should publish events and store structured observations only after the data model and safety rules are defined.

LLM Integration

LLM integration is not active.

Future LLM calls should be added only behind clear interfaces. They should not replace deterministic skills for answers already known from memory, such as user profile recall.

Raspberry Pi Deployment

Deployment scripts already exist, but no new Raspberry Pi deployment work should begin until the roadmap and architecture plan are approved.

Testing Boundary

Every architecture change must keep this suite passing:

```powershell
py -m pytest
py -m compileall core interfaces events memory skills scripts
py scripts\verify_phase2_events_memory.py
```
