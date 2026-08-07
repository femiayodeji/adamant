import type { Metadata, Viewport } from 'next';
import { Barlow_Condensed } from 'next/font/google';

import './globals.css';

/**
 * One display face, two weights, headings only — body copy stays on the system
 * sans and every number stays on the mono. Condensed because the words that need
 * the most room here are the ones set largest (a headline, a KO title, a corner
 * name), and because it sits closer to signage than to a web page.
 *
 * `next/font` downloads it at build time and serves it from our own origin, so a
 * deployed arena makes no request to Google and works with no network at all.
 */
const display = Barlow_Condensed({
  subsets: ['latin'],
  weight: ['500', '700'],
  variable: '--display',
  display: 'swap',
});

export const metadata: Metadata = {
  title: 'ADAM Arena',
  description: 'Two LLM agents fight under boxing rules. Create a match, share the code.',
};

export const viewport: Viewport = {
  themeColor: '#04070e',
  width: 'device-width',
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={display.variable}>
      <body>{children}</body>
    </html>
  );
}
