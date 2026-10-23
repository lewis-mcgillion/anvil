import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
const state=JSON.parse(readFileSync(new URL('./published.json',import.meta.url)));
const owner='lewis-mcgillion',repo='anvil';
function api(method,path,body){const args=['api','-X',method,path];if(body)args.push('--input','-');return execFileSync('gh',args,{input:body?JSON.stringify(body):undefined,encoding:'utf8',stdio:['pipe','pipe','pipe']});}
for(const [epicId,epic] of Object.entries(state.epics)){
  const issueNumbers=Object.entries(state.tasks).filter(([,task])=>task.parent===epic.number).map(([,task])=>task.number);
  const taskLinks=issueNumbers.map(number=>`- [ ] #${number}`).join('\n');
  const body=`## Outcome\nDeliver the ${epicId} subsystem in accordance with docs/architecture.md.\n\n## Implementation tasks\n${taskLinks}\n\n## Parent initiative\n#1\n\n## Exit gate\nAll child tasks pass acceptance tests, architecture/security requirements, and documented performance gates.\n`;
  api('PATCH',`repos/${owner}/${repo}/issues/${epic.number}`,{body});
  console.log(`${epicId}: ${issueNumbers.length} task links`);
}
const epicLinks=Object.values(state.epics).map(epic=>`- [ ] #${epic.number}`).join('\n');
api('PATCH',`repos/${owner}/${repo}/issues/1`,{body:`# Anvil: full product initiative\n\n**Mission:** Make AI agents testable and debuggable as traditional software. Capture failures, replay safely, turn incidents into repeatable regression tests and evaluate improvements.\n\n## Epics\n${epicLinks}\n\n## Product specification\nSee docs/architecture.md for execution modes, architecture, APIs, data model, security, testing, acceptance gates and rollout.\n\n## Completion criteria\nAll 14 epics and 84 tasks are completed with integrated evidence. No original production tool is ever executed during replay, and playback is never misrepresented as full agent re-execution.\n`});
