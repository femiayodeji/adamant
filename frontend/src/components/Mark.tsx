/**
 * The ADAM Arena mark.
 *
 * The ring is the arena seen from above, the two nodes inside it are the corners,
 * and the hairline seam between them is the tick boundary they resolve across —
 * simultaneously, which is why neither node is in front of the other. It is drawn
 * from the same three colours the rest of the interface uses: cyan for the machine,
 * blue and red for the two agents.
 *
 * Nodes take their fill from `--a` / `--b`, so the mark can never disagree with the
 * corners it stands for; the ring takes `currentColor`, so it inherits whatever it
 * is set beside. No filters — a blur would cost a raster at favicon sizes, and the
 * glow is a flat low-opacity disc behind each node instead.
 */

export function Mark({ size = 22 }: { size?: number }) {
  return (
    <svg
      className="mark"
      width={size}
      height={size}
      viewBox="0 0 32 32"
      aria-hidden
      focusable="false"
    >
      <circle
        cx="16"
        cy="16"
        r="13"
        fill="none"
        stroke="currentColor"
        strokeOpacity="0.4"
        strokeWidth="1"
        strokeDasharray="3.4 2.6"
      />
      {/* The soft discs are the only animated part, and only in the hero — see
          `.hero-mark .glow` in globals.css. The nodes themselves never move. */}
      <circle className="glow a" cx="10.6" cy="16" r="6.4" fill="var(--a)" opacity="0.14" />
      <circle className="glow b" cx="21.4" cy="16" r="6.4" fill="var(--b)" opacity="0.14" />
      <circle cx="10.6" cy="16" r="3.3" fill="var(--a)" />
      <circle cx="21.4" cy="16" r="3.3" fill="var(--b)" />
      <line
        x1="16"
        y1="4.6"
        x2="16"
        y2="27.4"
        stroke="currentColor"
        strokeOpacity="0.55"
        strokeWidth="1"
      />
    </svg>
  );
}
