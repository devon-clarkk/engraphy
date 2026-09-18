# Reference harness prompts

`answer.md`, `judge.md` and `judge_system.md` are the LoCoMo answer prompt, judge
prompt and judge system prompt of
[mem0ai/memory-benchmarks](https://github.com/mem0ai/memory-benchmarks),
`benchmarks/locomo/prompts.py`, at commit
`4b61c5d31b9c668a12b4f5e78064248a02c82d2b`. They are licensed under the Apache
License 2.0, reproduced in [LICENSE](LICENSE), and are unmodified: they were
written out by executing that module and saving its string constants byte for
byte, so no retyping stands between them and the source.

| file | upstream constant | sha256 (LF) |
|---|---|---|
| `answer.md` | `ANSWER_GENERATION_PROMPT` | `79c9f09bcc8d5e9e8b7e9786af587b02a67d366ab79285fc148b73fd20f6297b` |
| `judge.md` | `JUDGE_PROMPT` (the form without evidence) | `d248e056d993725e28fba8d16ca7081f0b59deae272ef294f3c6b00d48eac02b` |
| `judge_system.md` | `JUDGE_SYSTEM_PROMPT` | `36c007917faf1ab84516cdca577fb523711a9b993706fbae8ae37806e6f9adcc` |

They are used only by `bench/reference_pass.py`, which reports a LoCoMo figure
measured under the reference harness conventions, beside Engraphy's own strict
figure. `bench/core/reference.py` lists what the pass reproduces from the
reference harness and every place it differs. `bench/tests/test_levers.py` checks
the hashes above.
