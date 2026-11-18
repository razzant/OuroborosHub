// The owner's hands on this run's own X display: real pointer clicks and keystrokes through xdotool (XTEST), so they
// reach whichever tab Chrome really has in front and focused, as a person's input would. Only the display and cookie
// file run.sh created are used; nothing is taken from the environment the harness was started in.
import { spawnSync } from 'node:child_process';

export class X11 {
  constructor({ display, xauthority, wm }) {
    this.env = { PATH: process.env.PATH || '/usr/bin:/bin', DISPLAY: display, XAUTHORITY: xauthority };
    this.wm = wm && wm !== 'none' ? wm : null;
    const version = this._run(['version'], 5_000);
    this.available = version.status === 0;
    this.version = version.stdout || version.error || version.stderr;
  }

  _run(args, timeout = 15_000) {
    const result = spawnSync('xdotool', args, { env: this.env, encoding: 'utf8', timeout });
    return {
      args: args.join(' '),
      status: result.status,
      stdout: (result.stdout || '').trim(),
      stderr: (result.stderr || '').trim(),
      error: result.error ? String(result.error.message || result.error) : undefined,
    };
  }

  geometry(id) {
    const result = this._run(['getwindowgeometry', '--shell', id], 5_000);
    const values = {};
    for (const line of result.stdout.split('\n')) {
      const [key, value] = line.split('=');
      if (key && value !== undefined)
        values[key.trim()] = Number(value);
    }
    return { x: values.X, y: values.Y, width: values.WIDTH, height: values.HEIGHT };
  }

  // The browser window: the largest visible window of Chrome's process (or, failing that, of a chrom* class).
  chromeWindow(pid) {
    const queries = [['search', '--sync', '--onlyvisible', '--pid', String(pid)], ['search', '--onlyvisible', '--class', 'chrom']];
    for (const query of queries) {
      const found = this._run(query, 15_000);
      const windows = found.stdout.split(/\s+/).filter(Boolean)
        .map((id) => ({ id, ...this.geometry(id) }))
        .filter((window) => window.width > 200 && window.height > 200)
        .sort((a, b) => b.width * b.height - a.width * a.height);
      if (windows.length)
        return { ...windows[0], query: query.join(' ') };
    }
    return null;
  }

  activate(id) {
    return this.wm ? this._run(['windowactivate', '--sync', id], 10_000) : this._run(['windowfocus', '--sync', id], 10_000);
  }

  click(x, y) {
    return this._run(['mousemove', '--sync', String(Math.round(x)), String(Math.round(y)), 'click', '1'], 10_000);
  }

  // Plain ASCII only (the smoke test types lower-case words, digits and spaces).
  type(text) {
    if (!/^[a-z0-9 ]+$/.test(text))
      throw new Error('the owner types only lower-case letters, digits and spaces here');
    return this._run(['type', '--delay', '60', text], 60_000);
  }
}
