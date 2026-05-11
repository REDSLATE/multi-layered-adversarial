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
                },
            },
            borderRadius: {
                none: "0",
            },
        },
    },
    plugins: [require("tailwindcss-animate")],
};
