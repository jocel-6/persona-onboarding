import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Persona",
  description: "Meet your Persona",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
