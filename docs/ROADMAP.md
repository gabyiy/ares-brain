ARES Roadmap

This roadmap describes the current state and planned direction. It is a planning document only; it does not introduce new runtime behavior.

Completed Phases

Phase 1: Modular Text Intelligence

- Modular intent router
- Greeting intent
- Goodbye intent
- Weather intent and provider
- News intent and provider
- Knowledge intent and provider
- Stock intent and Alpha Vantage provider
- HTTP client
- Cache system

Phase 2: Event and Memory Foundation

- Event bus
- Router lifecycle events
- Memory v1 interface
- Conversation turn storage from the text REPL
- Phase 2 verification script

Phase 3: Skill Foundation

- Base `Skill` interface
- `SkillContext`
- `SkillResponse`
- `SkillRegistry`
- `SkillManager`
- `SkillPlugin`
- Built-in skill plugin
- Built-in `TimeDateSkill`
- Text REPL skill fallback wiring

Phase 4: Long-Term Profile Memory Recall

- Persistent `UserProfileStore`
- User facts stored separately from conversation history
- Supported profile fact detection
- Built-in `MemoryRecallSkill`
- Priority skill routing for personal recall questions
- Automated pytest suite
- Strict engineering rules

Phase 4B: Tool Selection Foundation

- `ToolSelector`
- `ToolSelection`
- Confidence/scoring rules for local skills
- Priority selection before generic intents
- Fallback selection after normal intents
- Tests for current TimeDate/MemoryRecall skills
- Tests for current Calculator, Notes, and Tasks skill selection

Phase 4C: Local Calculator Skill

- Built-in `CalculatorSkill`
- Safe local arithmetic without `eval()`
- Addition, subtraction, multiplication, division, parentheses, decimals, and bounded powers
- Clear rejection for unsupported or unsafe input
- REPL routing through `ToolSelector` and `SkillManager`
- Automated calculator and REPL path tests

Phase 5: Local Notes Skill

- Persistent `NotesStore`
- Notes stored in `data/notes.json`
- Notes kept separate from conversation memory and user profile memory
- Built-in `NotesSkill`
- Add, list, search, delete-one, and confirmed delete-all commands
- Automated store, skill, selector, and REPL path tests

Phase 6: Local Tasks Skill

- Persistent `TasksStore`
- Tasks stored in `data/tasks.json`
- Tasks kept separate from conversation memory, user profile memory, and notes
- Built-in `TasksSkill`
- Add, list, mark done, delete-one, and clear-completed commands
- Optional due text storage without real scheduling
- No notifications or calendar integration
- Automated store, skill, selector, and REPL path tests

Phase 7: In-Memory Conversation Context

- `ConversationContextManager`
- Last 20 handled skill turns kept in RAM
- Turn fields: timestamp, user message, assistant response, detected skill
- Retrieval APIs for last message, last user message, last assistant message, last skill, history, and clear
- `SkillManager` records handled skill interactions automatically
- REPL uses shared in-memory context for skill turns
- No disk persistence, embeddings, GPT, external APIs, or voice integration

Phase 8: Structured Intent Parser

- `core.Intent`
- `core.IntentParser`
- Deterministic local intent parsing before ToolSelector runs
- Recognized intents: `calculate`, `note`, `task`, `memory_recall`, `time_date`, and `unknown`
- Entity extraction for local tools, including task text, due text, note actions, calculator expressions, and memory recall topics
- `SkillManager` consumes `Intent` objects before calling `ToolSelector`
- Skills declare `intent_names` for structured matching
- Automated parser, ToolSelector, SkillManager, and REPL path tests
- Hardened parser coverage for ambiguous local phrases such as `remember to buy milk`, note reminders, birthday recall, task actions, note actions, calculator requests, and unknown text
- No AI, GPT, embeddings, voice, or external API integration

Current State

ARES is currently a text-first assistant with deterministic routing, structured local intent parsing, deterministic skills, event publishing, conversation memory, user profile memory, local calculator arithmetic, persistent local notes, offline tasks, and short-term in-memory conversation context for handled skill turns.

The current active interface is:

- `interfaces.text_repl`

The current deterministic answer paths are:

- Intent modules for weather, news, knowledge, stocks, greetings, and goodbye
- `IntentParser` plus `ToolSelector` for time/date, memory recall, calculator arithmetic, notes, and tasks
- In-memory conversation context for recent handled skill turns

The current memory paths are:

- `MemoryStore` for conversation-style memory
- `UserProfileStore` for persistent user facts
- `NotesStore` for persistent local notes
- `TasksStore` for persistent offline tasks
- `ConversationContextManager` for RAM-only short-term skill context

Next Priorities

1. Create and approve detailed architecture decisions for the next implementation phase.
2. Define how roadmap items map to intents, skills, providers, or interfaces.
3. Add a company information provider only after the architecture decision is documented.
4. Add cryptocurrency support only after company information is stable and tested.
5. Improve local natural language parsing only with structured parser rules and tests that preserve current behavior.
6. Add the next local skill only after its contract is documented and approved.

What Must Not Be Started Yet

- No voice implementation.
- No GPT or LLM integration.
- No embeddings.
- No real task scheduling or notifications.
- No calendar integration.
- No Raspberry Pi deployment work.
- No new skills before the roadmap and architecture decision is approved.
- No AI parser or regex-only parser rewrite.
- No robotics or movement integration.
- No vision integration.
- No broad refactors of the router, memory, or skill system.

Testing Rules Before Each Phase

Before starting a new phase:

1. Pull latest `main`.
2. Confirm the working tree is clean.
3. Run the full verification suite:

```powershell
py -m pytest
py -m compileall core interfaces events memory skills scripts
py scripts\verify_phase2_events_memory.py
```

4. Do not proceed if any check fails.
5. Fix the root cause of failures before adding new work.
6. Do not skip, xfail, or weaken tests without explicit approval.
7. Update README and SESSION_HANDOFF for meaningful changes.
8. Commit logical changes separately.
9. Push only after all checks pass.

Near-Term Planning Questions

- Should company information be an intent, a skill, a provider, or a combination?
- Which data provider should be used for company facts?
- What should be cached, and for how long?
- Which answers should remain deterministic and avoid LLM calls?
- How should profile memory be used in future skills without leaking private facts?

Exit Criteria For Next Implementation Phase

The next implementation phase can begin only after:

- `docs/ARCHITECTURE.md` is current.
- `docs/ROADMAP.md` is current.
- `docs/ENGINEERING_RULES.md` is followed.
- The full verification suite passes.
- The scope is limited to one logical capability.
