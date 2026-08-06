import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));

/** @type {import('next').NextConfig} */
const nextConfig = {
  // Pin the build root: there is an unrelated package-lock.json in the home
  // directory above the repo, and Turbopack otherwise warns about which one wins.
  turbopack: { root: here },
  // Static export: `next build` writes a self-contained site to ./out,
  // which FastAPI serves as static files (plan.md Tech Stack).
  output: 'export',
  // No Next image server in a static export.
  images: { unoptimized: true },
  // The app is a hash-routed SPA served from "/" — one HTML document.
  trailingSlash: false,
  reactStrictMode: true,
};

export default nextConfig;
