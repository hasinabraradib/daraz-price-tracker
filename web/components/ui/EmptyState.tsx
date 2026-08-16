export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-card border border-dashed border-border-strong px-6 py-12 text-center">
      <h3 className="font-display text-lg text-ink">{title}</h3>
      <p className="max-w-sm text-sm text-ink-muted">{description}</p>
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}
