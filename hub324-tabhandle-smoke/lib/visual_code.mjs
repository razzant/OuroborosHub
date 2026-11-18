// The picture each fixture tab paints, and how a screenshot of it is read back.
//
// The top 60% of the viewport is a grid of 8 x 6 flat cells. Row 0 says which tab painted it (A: blue/yellow,
// B: green/red). Rows 1-4 carry a 32-bit value the test chose just before the capture, most significant bit first
// (white 1, black 0). Row 5 is a check byte bound to the role: the XOR of the four value bytes and 0x5a (A) or
// 0xb5 (B). Cells are sampled in their central 40% by position relative to the image, so a capture that upstream
// scales down still reads; anything that is not one flat palette colour there makes the cell unreadable.
export const COLUMNS = 8;
export const ROWS = 6;
export const GRID_HEIGHT = 0.6;
export const PALETTE = Object.freeze({
  black: [0, 0, 0],
  white: [255, 255, 255],
  red: [220, 40, 40],
  green: [40, 180, 60],
  blue: [40, 80, 220],
  yellow: [235, 210, 40],
});
// A sampled cell counts as a palette colour only this close to it (largest channel difference), and only if no
// sampled pixel strays further than MAX_SPREAD from the cell's mean.
export const MAX_DISTANCE = 70;
export const MAX_SPREAD = 60;

// Self-contained on purpose: the fixture writes cellsFor.toString() into the pages, so page and reader share it.
export function cellsFor(role, value) {
  const identity = role === 'A' ? ['blue', 'yellow'] : role === 'B' ? ['green', 'red'] : null;
  if (!identity)
    throw new Error('role must be A or B');
  const v = value >>> 0;
  const cells = [];
  for (let column = 0; column < 8; column++)
    cells.push(identity[column % 2]);
  for (let bit = 31; bit >= 0; bit--)
    cells.push((v >>> bit) & 1 ? 'white' : 'black');
  const check = ((v >>> 24) ^ (v >>> 16) ^ (v >>> 8) ^ v ^ (role === 'A' ? 0x5a : 0xb5)) & 0xff;
  for (let bit = 7; bit >= 0; bit--)
    cells.push((check >>> bit) & 1 ? 'white' : 'black');
  return cells;
}

const NAMES = Object.keys(PALETTE);

function nearest(mean) {
  let best = null;
  let distance = Infinity;
  for (const name of NAMES) {
    const colour = PALETTE[name];
    const d = Math.max(Math.abs(mean[0] - colour[0]), Math.abs(mean[1] - colour[1]), Math.abs(mean[2] - colour[2]));
    if (d < distance) {
      best = name;
      distance = d;
    }
  }
  return { name: best, distance };
}

export function readCells({ width, height, rgba }) {
  const cells = [];
  const cellWidth = width / COLUMNS;
  const cellHeight = (height * GRID_HEIGHT) / ROWS;
  for (let row = 0; row < ROWS; row++) {
    for (let column = 0; column < COLUMNS; column++) {
      const x0 = Math.floor((column + 0.3) * cellWidth);
      const x1 = Math.max(x0 + 1, Math.floor((column + 0.7) * cellWidth));
      const y0 = Math.floor((row + 0.3) * cellHeight);
      const y1 = Math.max(y0 + 1, Math.floor((row + 0.7) * cellHeight));
      const sum = [0, 0, 0];
      let n = 0;
      for (let y = y0; y < y1; y++) {
        for (let x = x0; x < x1; x++) {
          const i = (y * width + x) * 4;
          sum[0] += rgba[i];
          sum[1] += rgba[i + 1];
          sum[2] += rgba[i + 2];
          n++;
        }
      }
      const mean = sum.map((s) => s / n);
      let spread = 0;
      for (let y = y0; y < y1; y++) {
        for (let x = x0; x < x1; x++) {
          const i = (y * width + x) * 4;
          for (let k = 0; k < 3; k++)
            spread = Math.max(spread, Math.abs(rgba[i + k] - mean[k]));
        }
      }
      const { name, distance } = nearest(mean);
      cells.push({
        name: distance <= MAX_DISTANCE && spread <= MAX_SPREAD ? name : null,
        mean: mean.map((m) => Math.round(m)),
        distance: Math.round(distance),
        spread: Math.round(spread),
      });
    }
  }
  return cells;
}

// { ok, role: 'A' | 'B' | null, value: uint32 | null, checkOk, unreadable, size, cells }.
// ok means: the identity row is exactly one role's, every value and check cell is black or white, and the check byte
// is the one that role writes for that value.
export function decodeGrid(image) {
  if (!image || image.width < COLUMNS * 4 || image.height < ROWS * 8)
    return { ok: false, role: null, value: null, checkOk: false, unreadable: COLUMNS * ROWS, reason: 'image too small' };
  const cells = readCells(image);
  const names = cells.map((cell) => cell.name);
  const unreadable = names.filter((name) => name === null).length;
  const role = ['A', 'B'].find((candidate) => cellsFor(candidate, 0).slice(0, COLUMNS).every((name, i) => names[i] === name)) ?? null;
  const binary = names.slice(COLUMNS).every((name) => name === 'black' || name === 'white');
  const value = binary ? names.slice(8, 40).reduce((acc, name) => acc * 2 + (name === 'white' ? 1 : 0), 0) : null;
  const check = binary ? names.slice(40, 48).reduce((acc, name) => acc * 2 + (name === 'white' ? 1 : 0), 0) : null;
  const checkOk = role !== null && value !== null && cellsFor(role, value).slice(40).every((name, i) => names[40 + i] === name);
  return {
    ok: role !== null && checkOk,
    role,
    value,
    check,
    checkOk,
    unreadable,
    size: [image.width, image.height],
    cells: names,
    worst: cells.reduce((worst, cell) => Math.max(worst, cell.distance), 0),
  };
}

export const hex32 = (value) => (value === null || value === undefined ? null : (value >>> 0).toString(16).padStart(8, '0'));
