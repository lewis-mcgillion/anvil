import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { epics } from './issues.mjs';

const state = JSON.parse(readFileSync(new URL('./published.json', import.meta.url)));
const repository = 'repos/lewis-mcgillion/anvil/issues';
const languageNote = 'Runtime stack decision (October 23, 2026): implement the API, workers, persistence access, replay engine and CLI in Python 3.12 with FastAPI, Pydantic v2, SQLAlchemy 2/asyncpg, pytest and Typer. Preserve the published OpenAPI/JSON schema contracts, PostgreSQL RLS and fail-closed replay invariants. Go commits from Epics 1-3 are migration reference only, not the target implementation. See docs/architecture.md.';

function api(method, number, payload) {
  return JSON.parse(execFileSync('gh', ['api', '-X', method, `${repository}/${number}`, '--input', '-'], {
    input: JSON.stringify(payload), encoding: 'utf8', timeout: 60000,
  }));
}

for (let epicIndex = 0; epicIndex < epics.length; epicIndex++) {
  const [epicTitle, , tasks] = epics[epicIndex];
  const epicId = `E${String(epicIndex + 1).padStart(2, '0')}`;
  const epic = state.epics[epicId];
  const epicNumber = epic.number;
  const epicBody = execFileSync('gh', ['issue', 'view', String(epicNumber), '-R', 'lewis-mcgillion/anvil', '--json', 'body', '--jq', '.body'], { encoding: 'utf8' });
  const heading = '## Runtime migration (Python)';
  const updatedBody = epicBody.includes(heading) ? epicBody.replace(/\n## Runtime migration \(Python\)[\s\S]*$/, '') : epicBody.trimEnd();
  api('PATCH', epicNumber, { title: `[${epicId}] ${epicTitle}`, body: `${updatedBody}\n\n${heading}\n${languageNote}\n` });
  console.log(`Updated epic #${epicNumber}`);

  for (let taskIndex = 0; taskIndex < tasks.length; taskIndex++) {
    const taskId = `ANVIL-${String(1 + epicIndex * 6 + taskIndex).padStart(3, '0')}`;
    const [title, goal, acceptance] = tasks[taskIndex];
    const issueNumber = state.tasks[taskId].number;
    const issueBody = execFileSync('gh', ['issue', 'view', String(issueNumber), '-R', 'lewis-mcgillion/anvil', '--json', 'body', '--jq', '.body'], { encoding: 'utf8' });
    const goalUpdated = issueBody.replace(/## Goal\n[\s\S]*?\n\n## Implementation requirements/, `## Goal\n${goal}\n\n## Implementation requirements`);
    const acceptanceUpdated = goalUpdated.replace(/## Acceptance criteria\n- [^\n]*/, `## Acceptance criteria\n- ${acceptance}`);
    const issueWithoutMigration = acceptanceUpdated.includes(heading) ? acceptanceUpdated.replace(/\n## Runtime migration \(Python\)[\s\S]*$/, '') : acceptanceUpdated.trimEnd();
    api('PATCH', issueNumber, { title: `[${taskId}] ${title}`, body: `${issueWithoutMigration}\n\n${heading}\n${languageNote}\n` });
    console.log(`Updated task #${issueNumber}`);
  }
}
