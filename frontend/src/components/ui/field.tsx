import { cn } from '@/lib/utils'

/**
 * A labelled input. Hand-written rather than generated, because the two shadcn
 * components this replaces would arrive with variants nothing here uses.
 */
export function Field({
  id,
  label,
  hint,
  className,
  ...props
}: React.ComponentProps<'input'> & { label: string; hint?: string }) {
  return (
    <div className="grid gap-1.5">
      <label htmlFor={id} className="text-sm font-medium text-foreground">
        {label}
      </label>
      <input
        id={id}
        aria-describedby={hint ? `${id}-hint` : undefined}
        className={cn(
          'h-9 rounded-lg border border-input bg-background px-3 text-sm outline-none',
          'placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3',
          'focus-visible:ring-ring/50 disabled:opacity-50',
          className,
        )}
        {...props}
      />
      {hint ? (
        <p id={`${id}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </p>
      ) : null}
    </div>
  )
}

/** An error the user has to read before anything else on the screen means much. */
export function Notice({ children, tone = 'error' }: { children: React.ReactNode; tone?: 'error' | 'ok' }) {
  return (
    <p
      role={tone === 'error' ? 'alert' : 'status'}
      className={cn(
        'rounded-lg border px-3 py-2 text-sm',
        tone === 'error'
          ? 'border-destructive/40 bg-destructive/10 text-destructive'
          : 'border-border bg-muted text-foreground',
      )}
    >
      {children}
    </p>
  )
}
