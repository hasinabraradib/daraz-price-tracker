import { cn } from "@/lib/cn";

type Tone = "neutral" | "accent" | "danger";

const toneClasses: Record<Tone, string> = {
  neutral: "bg-black/[0.04] text-ink-muted",
  accent: "bg-accent-soft text-accent",
  danger: "bg-danger-soft text-danger",
};

export function Badge({
  tone = "neutral",
  children,
  className,
}: {
  tone?: Tone;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium",
        toneClasses[tone],
        className
      )}
    >
      {children}
    </span>
  );
}
