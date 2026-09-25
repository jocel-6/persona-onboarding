import type { MetadataRoute } from "next";

// #12: installable, standalone, phone-first, like the band it pairs with.
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Persona",
    short_name: "Persona",
    description: "Your personal AI assistant. Talk to it the way you'd talk to your friend.",
    start_url: "/",
    display: "standalone",
    orientation: "portrait",
    background_color: "#ffffff",
    theme_color: "#ffffff",
    icons: [
      { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png" },
      { src: "/icons/icon-maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
  };
}
