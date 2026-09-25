import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Persona",
  description: "Meet your Persona",
  applicationName: "Persona",
  appleWebApp: { capable: true, title: "Persona", statusBarStyle: "default" },
  icons: { apple: "/icons/apple-touch-icon.png", icon: "/icons/icon-192.png" },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover", // draw under the notch; safe-area padding keeps content clear
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#000000" },
  ],
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
