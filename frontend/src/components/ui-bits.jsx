import React from "react";

export function PageHeader({ eyebrow, title, sub, right, testid }) {
  return (
    <div
      className="flex items-end justify-between gap-4 mb-6 pb-4 border-b border-rd-border"
      data-testid={testid}
    >
      <div>
        <div className="label-eyebrow mb-2">{eyebrow}</div>
        <h1 className="font-display text-3xl font-black tracking-tighter leading-none">
          {title}
        </h1>
        {sub && (
          <p className="text-xs text-rd-muted mt-2 font-mono leading-relaxed max-w-2xl">
            {sub}
          </p>
        )}
      </div>
      {right}
    </div>
  );
}

export function Card({ children, className = "", accentColor, testid }) {
  return (
    <div
      className={`bg-rd-bg2 border border-rd-border p-5 ${className}`}
      style={accentColor ? { borderTop: `2px solid ${accentColor}` } : undefined}
      data-testid={testid}
    >
      {children}
    </div>
  );
}

export function StatRow({ label, value, mono = true, testid }) {
  return (
    <div
      className="flex items-baseline justify-between py-1.5 border-b border-rd-border last:border-b-0"
      data-testid={testid}
    >
      <span className="text-[10px] uppercase tracking-widest text-rd-dim">
        {label}
      </span>
      <span
        className={`text-sm ${mono ? "font-mono" : "font-display"} text-rd-text`}
      >
        {value}
      </span>
    </div>
  );
}

export function Badge({ children, color = "#52525B", testid }) {
  return (
    <span
      className="inline-block px-2 py-0.5 text-[10px] font-mono uppercase tracking-widest border bg-transparent"
      style={{ color, borderColor: color }}
      data-testid={testid}
    >
      {children}
    </span>
  );
}

export function EmptyState({ message = "No records yet.", testid }) {
  return (
    <div
      className="border border-dashed border-rd-border bg-rd-bg2 px-6 py-10 text-center text-xs text-rd-dim uppercase tracking-widest"
      data-testid={testid}
    >
      {message}
    </div>
  );
}

export function LoadingRow({ testid }) {
  return (
    <div
      className="border border-rd-border bg-rd-bg2 px-6 py-8 text-center text-xs text-rd-dim uppercase tracking-widest"
      data-testid={testid}
    >
      <span className="inline-block w-2 h-2 bg-rd-warn pulse-dot mr-2 align-middle" />
      Loading
    </div>
  );
}
