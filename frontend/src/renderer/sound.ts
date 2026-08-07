/**
 * Match audio: the five impacts, synthesised, with no asset files.
 *
 * Every sound here is built from oscillators and one buffer of white noise, for
 * the same reason the fourteen clips are keyframe dicts in JSON rather than
 * authored FBX: it stays reviewable as text, it costs the static export nothing,
 * and a spectator on a phone downloads no megabytes to hear a punch land. There
 * is no sample library and no audio dependency — `fx.ts` is deliberately not a
 * particle system, and this is deliberately not a sound engine.
 *
 * Three things are load-bearing.
 *
 * OFF BY DEFAULT, AND A GESTURE TURNS IT ON. A shared watch link opens straight
 * into a live fight, and a page that starts making noise on its own is a page
 * people close. Browsers agree: an AudioContext created outside a user gesture
 * starts `suspended` and stays there. So `setEnabled(true)` is expected to be
 * called from a click — and when it is not (a restored preference on mount), the
 * context is created anyway and `armUnlock` resumes it on the first gesture the
 * page sees. Nothing plays before then; nothing is queued up to play late either.
 *
 * IT RIDES ON THE EVENT LIST, NOT ON THE CLIPS. `ArenaRenderer.playImpacts` calls
 * these methods from the same switch that fires the sparks, the flash and the
 * shake, so a sound cannot describe a different fight from the one on screen. In
 * particular a voided punch makes the jam sound and never the hit sound, for
 * exactly the reason it plays the `void` clip and never the strike clip.
 *
 * A VOICE BUDGET, BECAUSE THE RENDERER CATCHES UP. Ticks resolve faster than
 * clips play, so the queue speeds up under backlog and impacts can arrive in a
 * burst. Past `MAX_VOICES` simultaneous sources new sounds are dropped rather
 * than mixed, which keeps a flurry sounding like a flurry instead of like clipping.
 */

/** Master level. Low: this plays under a fight someone may be talking over. */
const MASTER_GAIN = 0.35;

/** Concurrent sources past which new sounds are dropped. */
const MAX_VOICES = 14;

/** Seconds of white noise held for the slaps, blocks and whiffs. Read from a
 *  random offset each time so repeated hits are not the same slice twice. */
const NOISE_SECONDS = 2;

/** The largest single hit in the table (`hook`), and so the top of the force
 *  scale. Mirrors `ATTACKS` in server/engine/rules.py; a hit above it just
 *  saturates rather than reading as a different sound. */
const HARDEST_HIT = 15;

type Source = OscillatorNode | AudioBufferSourceNode;

const clamp01 = (n: number): number => Math.min(1, Math.max(0, n));

export class Sound {
  private ctx: AudioContext | null = null;
  private master: GainNode | null = null;
  private noise: AudioBuffer | null = null;
  private voices = 0;
  private unlock: (() => void) | null = null;

  private on = false;

  get enabled(): boolean {
    return this.on;
  }

  /**
   * Turn the audio on or off.
   *
   * Called from a click this both opens the context and starts it. Called from
   * anywhere else — a preference restored on mount — it opens a context that the
   * browser holds suspended, and arms a one-shot listener to start it at the
   * first gesture. Either way `enabled` reports what the viewer asked for, and
   * `live()` is what decides whether anything can actually be heard.
   */
  setEnabled(on: boolean): void {
    this.on = on;
    if (!on) {
      this.silence();
      return;
    }
    const ctx = this.open();
    if (!ctx || !this.master) return;
    this.master.gain.setValueAtTime(MASTER_GAIN, ctx.currentTime);
    void ctx.resume().then(
      () => {
        if (ctx.state !== 'running') this.armUnlock(ctx);
      },
      () => this.armUnlock(ctx),
    );
  }

  dispose(): void {
    this.disarmUnlock();
    void this.ctx?.close().catch(() => {});
    this.ctx = null;
    this.master = null;
    this.noise = null;
  }

  /* ------------------------------------------------------------- the sounds */

  /**
   * A punch landing on a body: a low thump with a bright slap on the front of it.
   *
   * `damage` drives both, and drives them differently — the thump gets louder and
   * lower, the slap gets brighter. That pairing is what makes a hook audibly a
   * hook rather than a loud jab, without either sound naming an action.
   */
  hit(damage: number): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;
    const force = clamp01(damage / HARDEST_HIT);

    const thumpEnv = this.env(ctx, t, 0.55 + 0.4 * force, 0.006, 0.17 + 0.09 * force);
    const thump = ctx.createOscillator();
    thump.type = 'sine';
    thump.frequency.setValueAtTime(200 - 55 * force, t);
    thump.frequency.exponentialRampToValueAtTime(48, t + 0.16);
    thump.connect(thumpEnv);
    this.run(thump, t, t + 0.3);

    const slapEnv = this.env(ctx, t, 0.2 + 0.22 * force, 0.003, 0.08);
    const tone = ctx.createBiquadFilter();
    tone.type = 'lowpass';
    tone.frequency.setValueAtTime(950 + 2400 * force, t);
    tone.frequency.exponentialRampToValueAtTime(420, t + 0.09);
    tone.connect(slapEnv);
    this.run(this.noiseInto(ctx, tone), t, t + 0.12);
  }

  /**
   * The guard holds. A short hard knock — narrow, wooden, no low end at all.
   *
   * It has to be unmistakable against `hit` within one tick of hearing it, which
   * is why it is the one sound with no bass in it: a block is the punch that did
   * not reach the body.
   */
  blocked(): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;

    const env = this.env(ctx, t, 0.42, 0.002, 0.055);
    const knock = ctx.createBiquadFilter();
    knock.type = 'bandpass';
    knock.frequency.setValueAtTime(760, t);
    knock.Q.value = 2.4;
    knock.connect(env);
    this.run(this.noiseInto(ctx, knock), t, t + 0.09);

    const body = ctx.createOscillator();
    body.type = 'triangle';
    body.frequency.setValueAtTime(300, t);
    body.frequency.exponentialRampToValueAtTime(180, t + 0.05);
    body.connect(this.env(ctx, t, 0.16, 0.002, 0.05));
    this.run(body, t, t + 0.08);
  }

  /** Air, not contact: a filtered whoosh sweeping down as the punch goes past.
   *  `riposte` adds the counter's own short tick on the end of it. */
  dodged(riposte: number): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;

    const env = this.env(ctx, t, 0.3, 0.05, 0.16);
    const air = ctx.createBiquadFilter();
    air.type = 'bandpass';
    air.frequency.setValueAtTime(1900, t);
    air.frequency.exponentialRampToValueAtTime(520, t + 0.2);
    air.Q.value = 1.1;
    air.connect(env);
    this.run(this.noiseInto(ctx, air), t, t + 0.24);

    if (riposte <= 0) return;
    const at = t + 0.11;
    const counter = ctx.createOscillator();
    counter.type = 'sine';
    counter.frequency.setValueAtTime(520, at);
    counter.frequency.exponentialRampToValueAtTime(190, at + 0.07);
    counter.connect(this.env(ctx, at, 0.28, 0.004, 0.08));
    this.run(counter, at, at + 0.14);
  }

  /**
   * The repeat cap firing (spec §4.6): a two-blip buzzer, deliberately electronic.
   *
   * Nothing was thrown and nothing landed, so it must not sound like contact at
   * all. It is the amber of the HUD pip and the void clip, in sound: the machine
   * refusing the input.
   */
  jam(): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;
    for (const [n, at] of [[0, t], [1, t + 0.1]] as const) {
      const blip = ctx.createOscillator();
      blip.type = 'square';
      blip.frequency.setValueAtTime(n === 0 ? 196 : 165, at);
      blip.connect(this.env(ctx, at, 0.14, 0.004, 0.06));
      this.run(blip, at, at + 0.09);
    }
  }

  /**
   * A fighter goes down: the deepest impact in the set, and then the bell.
   *
   * The bell is the one sound that is not an impact at all. It is what tells a
   * viewer who looked away that the fight is over, and it rings past the fall so
   * the end of the match has an end to it.
   */
  ko(): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;

    const boom = ctx.createOscillator();
    boom.type = 'sine';
    boom.frequency.setValueAtTime(150, t);
    boom.frequency.exponentialRampToValueAtTime(34, t + 0.55);
    boom.connect(this.env(ctx, t, 0.85, 0.008, 0.7));
    this.run(boom, t, t + 0.85);

    const floor = ctx.createBiquadFilter();
    floor.type = 'lowpass';
    floor.frequency.setValueAtTime(2600, t);
    floor.frequency.exponentialRampToValueAtTime(300, t + 0.3);
    floor.connect(this.env(ctx, t, 0.34, 0.004, 0.3));
    this.run(this.noiseInto(ctx, floor), t, t + 0.36);

    // Two detuned partials with a long tail — a struck bell, not a sine beep.
    const at = t + 0.34;
    for (const [hz, level] of [[524, 0.3], [787, 0.17]] as const) {
      const partial = ctx.createOscillator();
      partial.type = 'sine';
      partial.frequency.setValueAtTime(hz, at);
      partial.connect(this.env(ctx, at, level, 0.006, 1.5));
      this.run(partial, at, at + 1.6);
    }
  }

  /* ------------------------------------------------------------- plumbing */

  /** The context, if a sound could actually be heard right now. */
  private live(): AudioContext | null {
    if (!this.on || !this.ctx || !this.master) return null;
    if (this.ctx.state !== 'running') return null;
    if (this.voices >= MAX_VOICES) return null;
    return this.ctx;
  }

  private open(): AudioContext | null {
    if (this.ctx) return this.ctx;
    const Ctor =
      typeof window === 'undefined'
        ? undefined
        : window.AudioContext ??
          (window as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctor) return null;

    const ctx = new Ctor();
    const master = ctx.createGain();
    master.gain.value = MASTER_GAIN;
    master.connect(ctx.destination);

    const noise = ctx.createBuffer(1, Math.floor(ctx.sampleRate * NOISE_SECONDS), ctx.sampleRate);
    const channel = noise.getChannelData(0);
    for (let i = 0; i < channel.length; i++) channel[i] = Math.random() * 2 - 1;

    this.ctx = ctx;
    this.master = master;
    this.noise = noise;
    return ctx;
  }

  /** Cut the master rather than waiting for tails: turning sound off means now. */
  private silence(): void {
    if (!this.ctx || !this.master) return;
    this.master.gain.cancelScheduledValues(this.ctx.currentTime);
    this.master.gain.setValueAtTime(0, this.ctx.currentTime);
  }

  /**
   * An attack/decay envelope on the master, one per layer of one sound.
   *
   * Exponential ramps, so the decays sound like decays — and never to a true
   * zero, which `exponentialRampToValueAtTime` is not allowed to reach.
   */
  private env(
    ctx: AudioContext,
    at: number,
    peak: number,
    attack: number,
    decay: number,
  ): GainNode {
    const gain = ctx.createGain();
    gain.gain.setValueAtTime(0.0001, at);
    gain.gain.exponentialRampToValueAtTime(Math.max(peak, 0.0002), at + attack);
    gain.gain.exponentialRampToValueAtTime(0.0001, at + attack + decay);
    gain.connect(this.master!);
    return gain;
  }

  private noiseInto(ctx: AudioContext, dest: AudioNode): AudioBufferSourceNode {
    const src = ctx.createBufferSource();
    src.buffer = this.noise;
    src.connect(dest);
    return src;
  }

  /** Start a source, stop it at a known time, and hold it against the budget. */
  private run(node: Source, at: number, until: number): void {
    this.voices++;
    node.onended = () => {
      this.voices = Math.max(0, this.voices - 1);
    };
    // A random offset into the noise buffer; oscillators have no such argument.
    if ('buffer' in node) node.start(at, Math.random() * (NOISE_SECONDS - 0.5));
    else node.start(at);
    node.stop(until);
  }

  /** Resume the context at the first gesture the page sees, then get out of the way. */
  private armUnlock(ctx: AudioContext): void {
    if (this.unlock || typeof window === 'undefined') return;
    const resume = () => {
      this.disarmUnlock();
      if (this.on) void ctx.resume().catch(() => {});
    };
    this.unlock = resume;
    window.addEventListener('pointerdown', resume, { once: true });
    window.addEventListener('keydown', resume, { once: true });
  }

  private disarmUnlock(): void {
    if (!this.unlock || typeof window === 'undefined') return;
    window.removeEventListener('pointerdown', this.unlock);
    window.removeEventListener('keydown', this.unlock);
    this.unlock = null;
  }
}
