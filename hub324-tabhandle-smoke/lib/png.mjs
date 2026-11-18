// A small PNG reader (and a writer for the self-test). Only what Chrome and upstream Playwright produce is needed:
// 8-bit (or 16-bit) grey, RGB, palette, grey+alpha and RGBA images, not interlaced, all five scanline filters.
import zlib from 'node:zlib';

const SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const CHANNELS = { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 };

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++)
      c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

export function crc32(...buffers) {
  let c = 0xffffffff;
  for (const buffer of buffers) {
    for (let i = 0; i < buffer.length; i++)
      c = CRC_TABLE[(c ^ buffer[i]) & 0xff] ^ (c >>> 8);
  }
  return (c ^ 0xffffffff) >>> 0;
}

function paeth(a, b, c) {
  const p = a + b - c;
  const pa = Math.abs(p - a);
  const pb = Math.abs(p - b);
  const pc = Math.abs(p - c);
  return pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
}

// Returns { width, height, rgba: Uint8Array (4 bytes per pixel), header }. Throws on anything it does not read.
export function decodePng(buffer) {
  if (!Buffer.isBuffer(buffer) || buffer.length < 45 || !buffer.subarray(0, 8).equals(SIGNATURE))
    throw new Error('not a PNG file');
  let offset = 8;
  let header = null;
  let palette = null;
  const data = [];
  while (offset + 12 <= buffer.length) {
    const length = buffer.readUInt32BE(offset);
    const type = buffer.toString('latin1', offset + 4, offset + 8);
    if (offset + 12 + length > buffer.length)
      throw new Error(`truncated ${type} chunk`);
    const body = buffer.subarray(offset + 8, offset + 8 + length);
    if (crc32(buffer.subarray(offset + 4, offset + 8), body) !== buffer.readUInt32BE(offset + 8 + length))
      throw new Error(`bad CRC in ${type} chunk`);
    offset += 12 + length;
    if (type === 'IHDR') {
      header = {
        width: body.readUInt32BE(0), height: body.readUInt32BE(4), bitDepth: body[8], colorType: body[9],
        compression: body[10], filter: body[11], interlace: body[12],
      };
    } else if (type === 'PLTE') {
      palette = body;
    } else if (type === 'IDAT') {
      data.push(body);
    } else if (type === 'IEND') {
      break;
    }
  }
  if (!header)
    throw new Error('no IHDR chunk');
  const { width, height, bitDepth, colorType, interlace } = header;
  const channels = CHANNELS[colorType];
  if (!channels || !(bitDepth === 8 || (bitDepth === 16 && colorType !== 3)))
    throw new Error(`unsupported PNG: bit depth ${bitDepth}, colour type ${colorType}`);
  if (interlace !== 0)
    throw new Error('interlaced PNG is not supported');
  if (colorType === 3 && !palette)
    throw new Error('palette PNG without a PLTE chunk');
  if (!width || !height || width * height > 64 * 1024 * 1024)
    throw new Error(`implausible size ${width}x${height}`);
  const bytesPerSample = bitDepth / 8;
  const bpp = channels * bytesPerSample;
  const stride = width * bpp;
  const raw = zlib.inflateSync(Buffer.concat(data));
  if (raw.length < (stride + 1) * height)
    throw new Error('image data is shorter than the header says');
  const pixels = Buffer.alloc(stride * height);
  let previous = Buffer.alloc(stride);
  for (let y = 0; y < height; y++) {
    const filter = raw[y * (stride + 1)];
    const line = raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1));
    const out = pixels.subarray(y * stride, (y + 1) * stride);
    for (let x = 0; x < stride; x++) {
      const a = x >= bpp ? out[x - bpp] : 0;
      const b = previous[x];
      const c = x >= bpp ? previous[x - bpp] : 0;
      let value = line[x];
      if (filter === 1)
        value += a;
      else if (filter === 2)
        value += b;
      else if (filter === 3)
        value += (a + b) >> 1;
      else if (filter === 4)
        value += paeth(a, b, c);
      else if (filter !== 0)
        throw new Error(`unknown scanline filter ${filter} in row ${y}`);
      out[x] = value & 0xff;
    }
    previous = out;
  }
  const rgba = new Uint8Array(width * height * 4);
  for (let i = 0, s = 0; i < width * height; i++, s += bpp) {
    // For 16-bit samples the high (first) byte is taken.
    const sample = (k) => pixels[s + k * bytesPerSample];
    let r;
    let g;
    let b;
    let alpha = 255;
    if (colorType === 0) {
      r = g = b = sample(0);
    } else if (colorType === 2) {
      r = sample(0); g = sample(1); b = sample(2);
    } else if (colorType === 3) {
      const p = pixels[s] * 3;
      r = palette[p] ?? 0; g = palette[p + 1] ?? 0; b = palette[p + 2] ?? 0;
    } else if (colorType === 4) {
      r = g = b = sample(0); alpha = sample(1);
    } else {
      r = sample(0); g = sample(1); b = sample(2); alpha = sample(3);
    }
    const o = i * 4;
    rgba[o] = r;
    rgba[o + 1] = g;
    rgba[o + 2] = b;
    rgba[o + 3] = alpha;
  }
  return { width, height, rgba, header };
}

function chunk(type, body) {
  const head = Buffer.alloc(8);
  head.writeUInt32BE(body.length, 0);
  head.write(type, 4, 'latin1');
  const tail = Buffer.alloc(4);
  tail.writeUInt32BE(crc32(head.subarray(4), body), 0);
  return Buffer.concat([head, body, tail]);
}

// Writes 8-bit RGBA (channels 4) or RGB (channels 3) pixels; `filters` cycles per row (self-test coverage).
export function encodePng(width, height, pixels, { channels = 4, filters = [0] } = {}) {
  const stride = width * channels;
  const raw = Buffer.alloc((stride + 1) * height);
  let previous = Buffer.alloc(stride);
  for (let y = 0; y < height; y++) {
    const filter = filters[y % filters.length];
    const line = Buffer.from(pixels.buffer, pixels.byteOffset + y * stride, stride);
    raw[y * (stride + 1)] = filter;
    for (let x = 0; x < stride; x++) {
      const a = x >= channels ? line[x - channels] : 0;
      const b = previous[x];
      const c = x >= channels ? previous[x - channels] : 0;
      const prediction = filter === 1 ? a : filter === 2 ? b : filter === 3 ? (a + b) >> 1 : filter === 4 ? paeth(a, b, c) : 0;
      raw[y * (stride + 1) + 1 + x] = (line[x] - prediction) & 0xff;
    }
    previous = line;
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8;
  ihdr[9] = channels === 4 ? 6 : 2;
  return Buffer.concat([SIGNATURE, chunk('IHDR', ihdr), chunk('IDAT', zlib.deflateSync(raw)), chunk('IEND', Buffer.alloc(0))]);
}
