# Sample requests for app/spec/parser

Each `*.json` file holds:

- `input`: the request, either free text or a kickoff JSON object.
- `llm_output`: the canned Gemini reply used by the mocked unit test. It is `null` for kickoff JSON, which must never reach the LLM.
- `expected`: the RunSpec fields the parser must produce. Anything not listed keeps its default.

These five are placeholders. Replace or extend them with the real requests from the kickoff kit.
