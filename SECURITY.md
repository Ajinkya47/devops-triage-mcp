# Security model

## What is protected
GitHub repositories (no unapproved writes), credentials, and the integrity of triage results.

## Threats considered
1. Hostile issue text (prompt injection). Anyone can write an issue.
2. A manipulated or mistaken model proposing the wrong action or target.
3. Over-broad SQL or tool use by the model.
4. Credential leakage through logs, prompts, output, or the container.

## Controls
| Threat | Control |
|---|---|
| Injection | Issue text wrapped as untrusted, pattern flags surfaced, evidence scores capped when flagged |
| Invented or wrong targets | Proposals refused unless the agent saw that issue in the same run. The approval screen shows the issue's title |
| Unapproved writes | The agent has propose tools only. Approval is a separate program with typed confirmation, expiry, payload hash, and allowlists |
| SQL abuse | SELECT-only parser check, table and function allowlists, read-only connection, timeout, row cap |
| Runaway agent | Step, tool-call, token, and time limits, plus repeat detection |
| Credential exposure | Write token isolated in `.env.write`. The server process never receives the model key. Token shapes are masked in logs |
| No record of actions | Audit log shared by agent and human. Writes are fail-closed: no audit row, no write |
| Container escape of secrets or code | The image has no write token and no approval code. CI checks both on every push |

## Known gaps
- Pattern-based injection detection can be bypassed by rewording.
- Someone with shell access to the machine could run the approval program.
- The container can reach the internet, and its environment (model key, read token) is visible to
  anyone with Docker access on the host.
- The model chooses which issues to assess, so ranking quality varies.
- The server does not verify that a proposed issue exists. Only the approval screen checks.
- Local SQLite files are not tamper-proof.
- Evaluation covers two attack types, in English.

Report security issues privately through GitHub's security advisory form for this repository.