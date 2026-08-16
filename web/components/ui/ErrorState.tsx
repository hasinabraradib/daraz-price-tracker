import { Button } from "./Button";

export function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-card border border-danger/20 bg-danger-soft px-6 py-8 text-center">
      <p className="text-sm text-danger">{message}</p>
      {onRetry && (
        <Button variant="secondary" size="sm" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}

export function InlineError({ message }: { message: string }) {
  return <p className="text-xs text-danger">{message}</p>;
}
