// Build artifacts from the canonical SVG sources; no generated raster substitutes.
const fs = require("node:fs");
const path = require("node:path");
const sharp = require(process.argv[2]);
const source = process.argv[3];
const target = process.argv[4];
async function render() {
  const svg = fs.readFileSync(path.join(source, "logo.svg"));
  const menu = fs.readFileSync(path.join(source, "menubar.svg"));
  await sharp(svg).resize(256, 256).png().toFile(path.join(target, "logo.png"));
  await sharp(menu).resize(46, 46).png().toFile(path.join(target, "menubar.png"));
  const iconset = path.join(target, "CodeConnect.iconset");
  fs.mkdirSync(iconset);
  for (const size of [16, 32, 128, 256, 512]) {
    await sharp(svg).resize(size, size).png().toFile(path.join(iconset, `icon_${size}x${size}.png`));
    await sharp(svg).resize(size * 2, size * 2).png().toFile(path.join(iconset, `icon_${size}x${size}@2x.png`));
  }
}
render().catch(() => { console.error("SVG icon rendering failed"); process.exitCode = 1; });
