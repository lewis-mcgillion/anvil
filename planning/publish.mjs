import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { epics, milestoneForEpic } from './issues.mjs';

const repo = 'lewis-mcgillion/anvil';
const statePath = new URL('./published.json', import.meta.url);
const state = existsSync(statePath) ? JSON.parse(readFileSync(statePath, 'utf8')) : { epics: {}, tasks: {} };
function api(method, path, payload) {
  const args = ['api', '-X', method, path, '-H', 'Accept: application/vnd.github+json'];
  if (payload !== undefined) args.push('--input', '-');
  for (let attempt = 0; attempt < 4; attempt++) {
    try { return JSON.parse(execFileSync('gh', args, { input: payload === undefined ? undefined : JSON.stringify(payload), encoding: 'utf8', timeout: 60000, stdio: ['pipe', 'pipe', 'pipe'] })); }
    catch (error) {
      if (attempt === 3) throw new Error(`${method} ${path}: ${error.stderr?.toString() || error.message}`);
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, (attempt + 1) * 3000);
    }
  }
}
function save() { writeFileSync(statePath, JSON.stringify(state, null, 2) + '\n'); }
function detail(title, outcome, acceptance, id, dependencies = []) {
  return `## Goal\n${outcome}\n\n## Implementation requirements\n- Follow the contract, security and versioning rules in docs/architecture.md.\n- Make changes in the relevant component with minimal public surface area and explicit failures.\n- Preserve replay's no-production-tool-execution invariant.\n\n## Acceptance criteria\n- ${acceptance}\n- Document errors, cancellation and resource cleanup where applicable.\n- Update public schemas and compatibility documentation when interfaces change.\n\n## Tests\n- Write success, rejection, edge-case and regression tests for the stated acceptance criteria.\n- Run targeted unit/contract tests and relevant integration or end-to-end tests in CI.\n\n## Tracking\n- Task ID: ${id}.\n- Dependencies: ${dependencies.length ? dependencies.join(', ') : 'Epic foundations and shared contracts'}.\n- Parent epic is linked as a GitHub sub-issue.\n`;
}

for (let index = 0; index < epics.length; index++) {
  const [title, , tasks] = epics[index];
  const epicId = `E${String(index + 1).padStart(2, '0')}`;
  if (!state.epics[epicId]) {
    const body = `## Outcome\n${title}. Deliver a tested, documented subsystem of Anvil.\n\n## Scope\n${tasks.map((task, taskIndex) => `- ${String(taskIndex + 1).padStart(2, '0')}. ${task[0]}: ${task[1]}`).join('\n')}\n\n## Exit gate\nAll child tasks satisfy their acceptance criteria, contracts, tests and the cross-cutting release rules in docs/architecture.md. No replay path reaches original tools.\n\n## Parent\nPart of initiative #1.\n`;
    const issue = api('POST', `repos/${repo}/issues`, { title: `[${epicId}] ${title}`, body, labels: ['epic'], milestone: milestoneForEpic(index) });
    state.epics[epicId] = { number: issue.number, id: issue.id, url: issue.html_url };
    save();
    console.log(`Epic ${epicId}: #${issue.number}`);
  }
  for (let taskIndex = 0; taskIndex < tasks.length; taskIndex++) {
    const [taskTitle, description, acceptance] = tasks[taskIndex];
    const taskNumber = 1 + index * 6 + taskIndex;
    const taskId = `ANVIL-${String(taskNumber).padStart(3, '0')}`;
    if (state.tasks[taskId]) continue;
    const dependencies = taskIndex ? [`${epicId} previous foundation task`] : ['E01 shared contracts'];
    const issue = api('POST', `repos/${repo}/issues`, { title: `[${taskId}] ${taskTitle}`, body: detail(taskTitle, description, acceptance, taskId, dependencies), labels: ['task'], milestone: milestoneForEpic(index) });
    state.tasks[taskId] = { number: issue.number, id: issue.id, parent: state.epics[epicId].number };
    save();
    console.log(`${taskId}: #${issue.number}`);
  }
}
console.log(JSON.stringify({ epics: Object.keys(state.epics).length, tasks: Object.keys(state.tasks).length }));
