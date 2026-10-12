import type { Metadata } from "next";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";
import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: `Talk to ${process.env.OWNER_SHORT_NAME}'s AI Rep`,
  description:
    `Chat with or talk to ${process.env.OWNER_NAME}'s AI screening Rep — ` +
    `ask about ${process.env.OWNER_POSSESSIVE} background and book time with ${process.env.OWNER_SHORT_NAME}.`,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${GeistSans.variable} ${GeistMono.variable}`}>
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
