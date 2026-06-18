/** @type {import('tailwindcss').Config} */
module.exports = {
    darkMode: ["class"],
    content: ["./src/**/*.{js,jsx,ts,tsx}", "./public/index.html"],
    theme: {
        extend: {
            fontFamily: {
                mono: [
                    "JetBrains Mono",
                    "ui-monospace",
                    "SFMono-Regular",
                    "Menlo",
                    "monospace",
                ],
                display: ["Chivo", "system-ui", "sans-serif"],
            },
            colors: {
                rd: {
                    bg: "#0A0A0A",
                    bg2: "#111111",
                    bg3: "#1A1A1A",
                    border: "#27272A",
                    borderStrong: "#52525B",
                    text: "#FFFFFF",
                    muted: "#A1A1AA",
                    dim: "#71717A",
                    alpha: "#3B82F6",
                    camaro: "#F59E0B",
                    chevelle: "#10B981",
                    redeye: "#DC2626",
                    warn: "#FBBF24",
                    danger: "#EF4444",
                    // 2026-02-21: `rd-accent` was referenced across the
                    // codebase (ARM ALL button, Unified Pipeline toggle,
                    // Webull floor button, plus 50+ other call sites)
                    // but never defined here — so `bg-rd-accent`,
                    // `border-rd-accent`, and `text-rd-accent` all
                    // resolved to no color, leaving primary action
                    // buttons as transparent outlines. Vibrant gold
                    // matches the existing `rd-warn` family and is
                    // high-contrast on the #0A0A0A dark background.
                    accent: "#FACC15",
                    // Success ack color used by ON/OFF badges and
                    // confirmation tiles. Same green family as
                    // `rd-chevelle` but slightly punchier for state
                    // indicators that need to read at a glance.
                    success: "#22C55E",
                },
            },
            borderRadius: {
                none: "0",
            },
        },
    },
    plugins: [require("tailwindcss-animate")],
};
