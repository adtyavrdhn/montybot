# Migration readiness

Transfer the existing repository to pydantic/montybot after code checks and deployment are green. Do not recreate it
from a clone: that would omit issues, PR discussion and other GitHub metadata. This note is preparation, not approval
to transfer the repository or change organisation access.

## Before the transfer

- No unmerged implementation PRs. Confirm final main CI and deployment, not only pre-merge checks.
- Record the deployed commit and retain the database, DBOS tables, encrypted jar, files and Monty sessions together.
- Keep ENCRYPTION_KEY and SESSION_SECRET unchanged. A new key would invalidate existing users' sign-ins or cookies.
- Verify U1-U6 on the hosted app with the basic-auth gate, not merely its unauthenticated health endpoint.
- Inventory Actions secrets without printing values. Current optional integration keys are TYPESAFE_API_KEY and
  LOGFIRE_TOKEN. Paid nightly evaluation additionally needs ANTHROPIC_API_KEY and explicit opt-in.
- Jev remains off until JEV_ENABLED=true and a key are present. Its recommendations are not automatic actions.
- Full Monty release tooling is merged, but private images must be built/activated on the VM before claiming the
  hosted app uses Full Monty. Keep source and image layers private; app-only deploys must not publish them.

## At and after the transfer

- Confirm pydantic permits the transfer and retains repository privacy, collaborator access, branch protections,
  Actions permissions, environments, secrets, variables and required checks. Verify these rather than assuming
  that an organisation's policies match the old owner's policies.
- Update local remotes and any consumers using adtyavrdhn/montybot. Audit workflow/API references, deployment scripts,
  docs, webhooks and integrations for the old name. GitHub redirects are not a substitute for updating automation.
- Confirm VM_SSH_KEY, VM_HOST, VM_USER and the verified host keys still work from Actions. Preserve the VM's env file
  and volumes. Trigger CI from the transferred repo before enabling its main deployment.
- Verify the web app, live WebSocket, streaming/reconnection, saved sign-ins and schedules again. Do not copy live
  cookies, page contents, account state or credentials into issues or test artifacts.
- Confirm Logfire receives only safe timing/usage metadata when enabled. Re-evaluate key access under org policy.

## Remaining evidence

The scripted fixture tests and Chromium checks establish code behavior, not universal success on real websites.
Live Jev calibration, full hosted user-path checks, the private Full Monty VM build, and Servo/bot-check/cost results
must be recorded separately. Sierra's Personal Agent Protocol announcement promises a later v0.1; no conformance
claim is possible until the actual specification is reviewed.
