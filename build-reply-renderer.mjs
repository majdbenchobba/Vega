import { readFile, writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const declared = JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8'));
const output = path.join(root, 'public/vendor');
const licenses = path.join(root, 'third-party');
await mkdir(output, { recursive: true });
await mkdir(licenses, { recursive: true });

for (const name of ['marked', 'entities']) {
  const directory = path.join(root, 'node_modules', name);
  const metadata = JSON.parse(await readFile(path.join(directory, 'package.json'), 'utf8'));
  if (declared.dependencies[name] !== metadata.version) throw new Error(`Install the pinned ${name} version before building.`);
  await writeFile(path.join(licenses, `${name}-LICENSE.txt`), await readFile(path.join(directory, 'LICENSE')));
}

await writeFile(path.join(output, 'marked.js'),
  await readFile(path.join(root, 'node_modules/marked/lib/marked.esm.js')));

const entityRoot = path.join(root, 'node_modules/entities/dist');
const copied = new Set();
async function copyEntityModule(relative) {
  if (copied.has(relative)) return;
  const sourcePath = path.resolve(entityRoot, relative);
  if (!sourcePath.startsWith(entityRoot + path.sep) || !relative.endsWith('.js')) {
    throw new Error('An entity module left its package boundary.');
  }
  const source = await readFile(sourcePath, 'utf8');
  copied.add(relative);
  const target = path.join(output, 'entities', relative);
  await mkdir(path.dirname(target), { recursive: true });
  await writeFile(target, source);
  for (const match of source.matchAll(/\bfrom\s+["'](\.[^"']+)["']/g)) {
    await copyEntityModule(path.normalize(path.join(path.dirname(relative), match[1])));
  }
}
await copyEntityModule('decode.js');
console.log(`Prepared pinned Markdown lexer and ${copied.size} entity-decoder modules for local serving.`);
