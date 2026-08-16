import type { AlertEvent } from "@/lib/types";
import { formatRelativeTime } from "@/lib/format";
import { Badge } from "@/components/ui/Badge";

export function AlertEventsFeed({ events }: { events: AlertEvent[] }) {
  if (events.length === 0) {
    return (
      <p className="text-sm text-ink-faint">
        Nothing has fired yet. Alerts show up here the moment a rule&apos;s condition is met.
      </p>
    );
  }

  return (
    <ul className="divide-y divide-border">
      {events.map((event) => (
        <li key={event.id} className="flex items-start justify-between gap-4 py-3">
          <div>
            <p className="text-sm text-ink">{event.message}</p>
            <p className="mt-0.5 text-xs text-ink-faint">{formatRelativeTime(event.triggered_at)}</p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {event.resolved_at && (
              <Badge tone="neutral">resolved</Badge>
            )}
            {event.delivery_status === "sent" && <Badge tone="accent">delivered</Badge>}
            {event.delivery_status === "failed" && <Badge tone="danger">delivery failed</Badge>}
            {event.delivery_status === "pending" && <Badge tone="neutral">pending</Badge>}
          </div>
        </li>
      ))}
    </ul>
  );
}
