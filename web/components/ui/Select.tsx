"use client";

import { SelectHTMLAttributes, forwardRef } from "react";
import { cn } from "@/lib/cn";

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  ({ className, children, ...props }, ref) => {
    return (
      <select
        ref={ref}
        className={cn(
          "w-full rounded-md border border-border-strong bg-surface px-3 py-2 text-sm text-ink",
          "transition-colors duration-150 ease-calm",
          "focus:outline-none focus:border-accent focus:ring-2 focus:ring-accent/30",
          className
        )}
        {...props}
      >
        {children}
      </select>
    );
  }
);
Select.displayName = "Select";
