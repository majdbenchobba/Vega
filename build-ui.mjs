import { stripTypeScriptTypes } from 'node:module';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const modules = [
  'renderer/vegaLiveVoice', 'renderer/vegaPresenceModel', 'renderer/vegaPcmSender',
  'renderer/voiceActivity', 'renderer/vegaBriefing', 'renderer/briefingNavigation',
  'renderer/planningRefresh', 'renderer/vegaVoiceReply', 'renderer/vegaCalendar',
  'shared/vegaBriefing', 'shared/vegaVoices', 'shared/spokenText',
];
for (const name of modules) {
  const source = await readFile(path.join(root, 'port/src', name + '.ts'), 'utf8');
  const output = path.join(root, 'public/policy', name + '.js');
  await mkdir(path.dirname(output), { recursive: true });
  const compiled = stripTypeScriptTypes(source, { mode: 'strip' })
    .replace(/(from\s+['"][^'"]+)\.ts(['"])/g, '$1.js$2');
  await writeFile(output, compiled);
}
for (const name of ['vegaPresence.css', 'vegaPcmWorklet.js']) {
  await writeFile(
    path.join(root, 'public', name),
    await readFile(path.join(root, 'port/src/renderer', name)),
  );
}
console.log(`Built ${modules.length} preserved policy modules and the voice worklet/presence styles.`);
await import('./build-reply-renderer.mjs');
await import('./build-dashboard.mjs');
