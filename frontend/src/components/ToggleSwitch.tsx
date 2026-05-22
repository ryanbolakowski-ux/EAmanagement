interface Props {
  checked: boolean
  onChange: (next: boolean) => void
  disabled?: boolean
  label?: string
}

/** Compact iOS-style on/off switch. */
export default function ToggleSwitch({ checked, onChange, disabled = false, label }: Props) {
  return (
    <label className="inline-flex items-center gap-2 cursor-pointer select-none">
      {label && <span className="text-xs font-medium text-slate-600 dark:text-slate-300">{label}</span>}
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => !disabled && onChange(!checked)}
        className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
          checked ? 'bg-green-500' : 'bg-slate-300 dark:bg-slate-700'
        } ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}
      >
        <span
          className={`inline-block h-4 w-4 transform rounded-full bg-white dark:bg-slate-800 shadow transition-transform ${
            checked ? 'translate-x-[18px]' : 'translate-x-0.5'
          }`}
        />
      </button>
    </label>
  )
}
