// Copy the shared SWIFT look (colour tokens, fonts, the official wordmark and icon) from SWIFT Director into the
// launcher, so there is one source for them. Run before starting or packaging. The copies are not committed.
import { cpSync, mkdirSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
const here = dirname(fileURLToPath(import.meta.url)), root = join(here, '..'), shared = join(root, '..', 'static'), out = join(root, 'renderer', 'shared');
mkdirSync(join(out, 'fonts'), { recursive: true });
cpSync(join(shared, 'swift-tokens.css'), join(out, 'swift-tokens.css'));
cpSync(join(shared, 'brand', 'swift-wordmark.svg'), join(out, 'swift-wordmark.svg'));
for (const f of readdirSync(join(shared, 'fonts'))) cpSync(join(shared, 'fonts', f), join(out, 'fonts', f));
cpSync(join(shared, 'brand', 'director-icon-512.png'), join(root, 'build', 'icon.png'));
console.log('Shared SWIFT assets copied.');
