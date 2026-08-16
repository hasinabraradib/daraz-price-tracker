"use client";

import { InputHTMLAttributes, forwardRef } from "react";
import { cn } from "@/lib/cn";

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  error?: boolean;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(
  ({ error, className, ...props }, ref) => {
    return (
      <input
        ref={ref}
        className={cn(
          "w-full rounded-md border bg-surface px-3 py-2 text-sm text-ink placeholder:text-ink-faint",
          "transition-colors duration-150 ease-calm",
          "focus:outline-none focus:ring-2 focus:ring-accent/30",
          error ? "border-danger focus:border-danger" : "border-border-strong focus:border-accent",
          className
        )}
        {...props}
      />
    );
  }
);
Input.displayName = "Input";

interface FieldProps {
  label: string;
  hint?: string;
  error?: string;
  children: React.ReactNode;
  htmlFor?: string;
}

export function Field({ label, hint, error, children, htmlFor }: FieldProps) {
  return (
    <div className="space-y-1.5">
      <label htmlFor={htmlFor} className="block text-sm font-medium text-ink">
        {label}
      </label>
      {children}
      {hint && !error && <p className="text-xs text-ink-muted">{hint}</p>}
      {error && <p className="text-xs text-danger">{error}</p>}
    </div>
  );
}
