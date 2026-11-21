# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately
through GitHub instead: open the repository's **Security** tab and choose
**Report a vulnerability**. We aim to reply within a week.

Please include the version or commit, the steps to reproduce, and what an
attacker could do with it.

## What to keep in mind

Lunatap drives real devices and simulators, and its agents send screenshots
and screen text to the model provider you configure. Do not point it at apps
or accounts that show data you may not share with that provider.

Keep API keys in `.env`, which git ignores. Never commit them.
