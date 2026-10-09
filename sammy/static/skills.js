// Skills (#128): the user's playbooks. Sammy sees the saved skills' names and loads one when a task fits it; drafts
// from "Teach Sammy" in the live view wait here until the user saves them. Loaded before app.js, whose helpers
// ($, api, element, button, report, telemetry) these functions use when they run.
'use strict';

const SKILL_FIELDS = [
  ['name', 'Name'],
  ['when_to_use', 'When to use it'],
  ['inputs', 'What it needs'],
  ['steps', 'Steps'],
  ['verify', 'How to check it worked'],
  ['returns', 'What to give back'],
  ['approvals', 'Ask first before'],
  ['failures', 'If something goes wrong'],
];
const REQUIRED_SKILL_FIELDS = ['name', 'when_to_use', 'steps'];

async function openSkills() {
  $('skills-title').focus();
  const skills = await api('/api/skills');
  $('skill-list').replaceChildren(...(skills.length ? skills.map(skillItem) : [element('li',
    "No skills yet. Take over Sammy's browser and press Teach Sammy to show it a task, or ask it to save what it just did.")]));
}

function skillItem(skill) {
  const about = element('span');
  about.append(element('strong', skill.name));
  if (skill.draft) about.append(' ', element('span', 'Draft', 'badge waiting'));
  about.append(element('span', skill.when_to_use, 'list-detail'));
  const form = skillForm(skill);
  const actions = element('span', '', 'list-actions');
  const edit = button('Edit', 'secondary', () => {
    form.hidden = !form.hidden;
    edit.setAttribute('aria-expanded', String(!form.hidden));
    if (!form.hidden) form.querySelector('input').focus();
  });
  edit.setAttribute('aria-expanded', 'false');
  edit.setAttribute('aria-label', `Edit ${skill.name}`);
  actions.append(edit);
  if (skill.draft) actions.append(button('Save', '', () => saveSkill(skill, skill)));
  actions.append(button('Delete', 'bad', async () => {
    if (!confirm(`Delete "${skill.name}"? Sammy will no longer use it.`)) return;
    await telemetry.span('delete skill', { skill_id: skill.id }, () => (
      api(`/api/skills/${skill.id}`, { method: 'DELETE' })));
    await openSkills();
  }));
  const item = element('li', '', 'skill');
  item.append(about, actions, form);
  return item;
}

function skillForm(skill) {
  const form = element('form', '', 'skill-form');
  form.hidden = true;
  for (const [key, label] of SKILL_FIELDS) {
    const field = element(key === 'name' ? 'input' : 'textarea');
    field.id = `skill-${skill.id}-${key}`;
    field.name = key;
    field.value = skill[key];
    field.required = REQUIRED_SKILL_FIELDS.includes(key);
    if (key !== 'name') field.rows = key === 'steps' ? 8 : 2;
    const labelled = element('label', label);
    labelled.htmlFor = field.id;
    form.append(labelled, field);
  }
  const save = element('button', skill.draft ? 'Save skill' : 'Save changes');
  save.type = 'submit';
  form.append(save);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    save.disabled = true;
    const changed = Object.fromEntries(SKILL_FIELDS.map(([key]) => [key, form.elements[key].value]));
    report(saveSkill(skill, changed).finally(() => { save.disabled = false; }));
  });
  return form;
}

async function saveSkill(skill, text) {
  // Saving always makes it a skill Sammy uses: a draft's review ends here.
  const body = { ...Object.fromEntries(SKILL_FIELDS.map(([key]) => [key, text[key]])), draft: false };
  await telemetry.span('save skill', { skill_id: skill.id, draft: skill.draft }, () => (
    api(`/api/skills/${skill.id}`, { method: 'PUT', body })));
  await openSkills();
}
