/**
 * Records a replay to a downloadable video file.
 *
 * Two things make this more than "hand the canvas to MediaRecorder".
 *
 * The HUD is DOM, not WebGL. HP bars, names, the tick counter and the event lines
 * all live in React over the canvas, so a raw canvas capture would produce a
 * fight with no scoreboard. Everything is therefore composited into a second 2D
 * canvas — `drawImage` the frame, then paint the HUD on top — and it is *that*
 * canvas that is recorded.
 *
 * And the WebGL drawing buffer is only readable inside the frame that drew it.
 * With `preserveDrawingBuffer` left off (it costs real performance on every frame
 * of every live match, recorded or not), `drawImage` from an independent rAF loop
 * yields black. `composite()` is therefore called from `ArenaRenderer`'s own frame
 * hook, immediately after `render()`.
 *
 * The audio arrives as a MediaStream from `sound.ts` and is added to the canvas
 * stream as a second track. It is tapped ahead of the mute control, so the file
 * has the punches in it even when the page was recorded silent — but the tracks
 * have to be married BEFORE `MediaRecorder.start()`, because a recorder samples
 * the stream it was constructed with and ignores tracks added afterwards. A
 * browser with no Web Audio simply records video, as it did before.
 */

/** Paints the scoreboard over one composited frame. */
export type OverlayPainter = (
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
) => void;

export interface VideoFormat {
  mime: string;
  /** File extension matching `mime` — what the download is actually named. */
  ext: 'mp4' | 'webm';
}

/**
 * The best container this browser can actually encode, MP4 first.
 *
 * Chromium 126+ and Safari emit real H.264 MP4; Firefox has no MP4 encoder and
 * only ever returns WebM. Callers surface `ext` to the user rather than promising
 * an MP4 and writing a WebM into it — a mislabelled file is worse than a labelled
 * fallback. `null` means this browser cannot record at all.
 *
 * Sound outranks the container. Every type that can carry audio is tried before
 * any that cannot, so a browser that encodes H.264 video but will not put AAC
 * beside it gets a WebM with the fight in it rather than a silent MP4 — which is
 * not hypothetical: Chromium accepts `video/mp4;codecs=avc1.42E01E` and rejects
 * the same string with `mp4a.40.2` appended. Naming the audio codec is the only
 * way to find that out; a bare type is accepted and then quietly drops the track.
 * The audio-less rows at the end are for a browser that records neither, and for
 * the case where there is no Web Audio to record.
 */
export function pickVideoFormat(): VideoFormat | null {
  if (typeof MediaRecorder === 'undefined') return null;
  const candidates: VideoFormat[] = [
    { mime: 'video/mp4;codecs=avc1.42E01E,mp4a.40.2', ext: 'mp4' },
    { mime: 'video/mp4;codecs=avc1,mp4a.40.2', ext: 'mp4' },
    { mime: 'video/webm;codecs=vp9,opus', ext: 'webm' },
    { mime: 'video/webm;codecs=vp8,opus', ext: 'webm' },
    { mime: 'video/mp4;codecs=avc1.42E01E', ext: 'mp4' },
    { mime: 'video/mp4', ext: 'mp4' },
    { mime: 'video/webm;codecs=vp9', ext: 'webm' },
    { mime: 'video/webm', ext: 'webm' },
  ];
  return candidates.find((c) => MediaRecorder.isTypeSupported(c.mime)) ?? null;
}

/** 720p on the long edge. Large enough to read the HUD text, small enough that a
 *  60-tick fight is a handful of megabytes. */
const TARGET_WIDTH = 1280;
const FPS = 30;
/** H.264 rejects odd dimensions, so both axes are rounded to even. */
const even = (n: number) => Math.max(2, Math.round(n / 2) * 2);

export class ReplayRecorder {
  readonly format: VideoFormat;
  private readonly canvas: HTMLCanvasElement;
  private readonly ctx: CanvasRenderingContext2D;
  private readonly chunks: Blob[] = [];
  private recorder: MediaRecorder | null = null;
  private stream: MediaStream | null = null;

  constructor(
    private readonly source: HTMLCanvasElement,
    private readonly painter: OverlayPainter,
    format: VideoFormat,
    /** The match audio, from `Sound.capture()`. `null` records a silent file. */
    private readonly audio: MediaStream | null = null,
  ) {
    this.format = format;
    const aspect = source.height / Math.max(source.width, 1) || 9 / 16;
    this.canvas = document.createElement('canvas');
    this.canvas.width = even(TARGET_WIDTH);
    this.canvas.height = even(TARGET_WIDTH * aspect);
    const ctx = this.canvas.getContext('2d');
    if (!ctx) throw new Error('2D canvas unavailable — cannot record.');
    this.ctx = ctx;
  }

  get width(): number {
    return this.canvas.width;
  }

  get height(): number {
    return this.canvas.height;
  }

  /** Whether the file being written actually has an audio track in it. */
  get hasAudio(): boolean {
    return (this.stream?.getAudioTracks().length ?? 0) > 0;
  }

  start(): void {
    // One black frame up front, so a recorder that samples before the first
    // composite never emits an undefined-content frame.
    this.ctx.fillStyle = '#05070a';
    this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);

    this.stream = this.canvas.captureStream(FPS);
    // Married before `start()`, never after — see the note at the top of the file.
    for (const track of this.audio?.getAudioTracks() ?? []) this.stream.addTrack(track);
    this.recorder = new MediaRecorder(this.stream, {
      mimeType: this.format.mime,
      videoBitsPerSecond: 6_000_000,
      audioBitsPerSecond: 128_000,
    });
    this.recorder.ondataavailable = (event) => {
      if (event.data.size > 0) this.chunks.push(event.data);
    };
    this.recorder.start(250);
  }

  /** Called from the renderer's frame hook — see the note at the top of the file. */
  composite = (): void => {
    const { width, height } = this.canvas;
    this.ctx.fillStyle = '#05070a';
    this.ctx.fillRect(0, 0, width, height);
    if (this.source.width > 0 && this.source.height > 0) {
      this.ctx.drawImage(this.source, 0, 0, width, height);
    }
    this.ctx.save();
    this.painter(this.ctx, width, height);
    this.ctx.restore();
  };

  stop(): Promise<Blob> {
    return new Promise((resolve) => {
      const recorder = this.recorder;
      if (!recorder || recorder.state === 'inactive') {
        resolve(new Blob(this.chunks, { type: this.format.mime }));
        return;
      }
      recorder.onstop = () => {
        for (const track of this.stream?.getTracks() ?? []) track.stop();
        resolve(new Blob(this.chunks, { type: this.format.mime }));
      };
      recorder.stop();
    });
  }

  dispose(): void {
    if (this.recorder && this.recorder.state !== 'inactive') this.recorder.stop();
    for (const track of this.stream?.getTracks() ?? []) track.stop();
    this.recorder = null;
    this.stream = null;
  }
}

/** Hand a finished recording to the browser as a download. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Revoked on a timer rather than immediately: Safari reads the object URL
  // asynchronously and a synchronous revoke cancels the download.
  setTimeout(() => URL.revokeObjectURL(url), 30_000);
}
