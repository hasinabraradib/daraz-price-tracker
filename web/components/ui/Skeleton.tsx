import { cn } from "@/lib/cn";

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("animate-pulse rounded-md bg-black/[0.06]", className)} />;
}

export function Spinner({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        "inline-block h-4 w-4 animate-spin rounded-full border-2 border-ink-faint border-t-accent",
        className
      )}
      aria-hidden="true"
    />
  );
}

export function PageLoading() {
  return (
    <div className="flex items-center justify-center py-24">
      <Spinner className="h-6 w-6" />
    </div>
  );
}
