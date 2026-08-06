/**
 * Put the Xbot rig where Next can export it, without committing a second copy.
 *
 * The rig is `statics/models/xbot.glb` — the original ADAM asset, already tracked.
 * `public/` is where Next picks assets up, so it has to exist there too, but
 * committing a byte-identical 2.9 MB blob would sit in git history forever.
 *
 * Runs as `prebuild`/`predev`, and is a no-op when the file is already in place —
 * which is the case inside the Docker frontend stage, where the build context is
 * `frontend/` alone and `../statics` does not exist.
 */
import { copyFileSync, existsSync, mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const dest = resolve(here, '..', 'public', 'models', 'xbot.glb');
const source = resolve(here, '..', '..', 'statics', 'models', 'xbot.glb');

if (existsSync(dest)) {
  process.exit(0);
}

if (!existsSync(source)) {
  console.error(
    `rig not found.\n  expected: ${source}\n  or already staged at: ${dest}\n` +
      'The renderer cannot build without it.',
  );
  process.exit(1);
}

mkdirSync(dirname(dest), { recursive: true });
copyFileSync(source, dest);
console.log(`rig staged -> ${dest}`);
