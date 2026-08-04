/**
 * @wellnesscrm/design-system — the public surface.
 *
 * 🔒 ADR-03 / NFR-056: this package is the first frontend code written, and no
 * feature may introduce a bespoke one-off component. Feature apps import from
 * here; they do not reach into `src/` paths, and they do not declare raw colour
 * or spacing values (enforced by the ESLint rule in `frontend/eslint.config.js`
 * and by `check_boundaries.py` R8).
 *
 * Layers, in dependency order (Arch §4.2):
 *
 *   tokens → primitives → patterns → layout shells
 *
 * A primitive never imports a pattern. A pattern may compose primitives — as
 * `ConfirmDialog` composes `Modal` and `Button`. Nothing imports upward.
 */

// ─── Tokens ──────────────────────────────────────────────────────────────
// The only place raw values exist.
export {
  tokens,
  colour,
  fontSize,
  fontWeight,
  lineHeight,
  fontFamily,
  space,
  radius,
  shadow,
  touchTarget,
  zIndex,
  duration,
  easing,
  breakpoint,
} from './tokens/tokens'
export type { Tokens } from './tokens/tokens'

// ─── Primitives ──────────────────────────────────────────────────────────
export { Button } from './primitives/Button'
export type { ButtonProps, ButtonVariant, ButtonSize } from './primitives/Button'

export { Input } from './primitives/Input'
export type { InputProps } from './primitives/Input'

export { Select } from './primitives/Select'
export type { SelectProps } from './primitives/Select'

export { Textarea } from './primitives/Textarea'
export type { TextareaProps } from './primitives/Textarea'

export { Checkbox } from './primitives/Checkbox'
export type { CheckboxProps } from './primitives/Checkbox'

export { Radio, RadioGroup } from './primitives/Radio'
export type { RadioProps, RadioGroupProps } from './primitives/Radio'

export { Modal } from './primitives/Modal'
export type { ModalProps } from './primitives/Modal'

export { ToastProvider, useToast } from './primitives/Toast'
export type { Toast, ToastTone, ToastProviderProps } from './primitives/Toast'

export {
  Table,
  TableHead,
  TableBody,
  TableRow,
  TableHeaderCell,
  TableCell,
} from './primitives/Table'
export type {
  TableProps,
  TableRowProps,
  TableHeaderCellProps,
  TableCellProps,
} from './primitives/Table'

export { Card, CardHeader, CardBody, CardFooter } from './primitives/Card'
export type { CardProps, CardHeaderProps } from './primitives/Card'

export { Badge } from './primitives/Badge'
export type { BadgeProps, BadgeTone } from './primitives/Badge'

export { Spinner } from './primitives/Spinner'
export type { SpinnerProps } from './primitives/Spinner'

export { Tabs, TabPanel } from './primitives/Tabs'
export type { TabsProps, TabPanelProps, TabItem } from './primitives/Tabs'

// ─── Patterns ────────────────────────────────────────────────────────────
// 🔒 EmptyState and ConfirmDialog exist because NFR-064 and NFR-065 apply to
// ~40 screens. As components they are free; as per-screen discipline they would
// not happen.
export { PageHeader } from './patterns/PageHeader'
export type { PageHeaderProps, Breadcrumb } from './patterns/PageHeader'

export { EmptyState } from './patterns/EmptyState'
export type { EmptyStateProps } from './patterns/EmptyState'

export { ErrorState } from './patterns/ErrorState'
export type { ErrorStateProps } from './patterns/ErrorState'

export { ConfirmDialog } from './patterns/ConfirmDialog'
export type { ConfirmDialogProps } from './patterns/ConfirmDialog'

export { FormField } from './patterns/FormField'
export type { FormFieldProps } from './patterns/FormField'

export { DataList } from './patterns/DataList'
export type { DataListProps, DataListItem } from './patterns/DataList'

// ─── Layout shells ───────────────────────────────────────────────────────
export { AppShell } from './layout/AppShell'
export type { AppShellProps, NavItem } from './layout/AppShell'

export { MobileShell } from './layout/MobileShell'
export type { MobileShellProps, MobileNavItem } from './layout/MobileShell'

export { AdminShell } from './layout/AdminShell'
export type { AdminShellProps, AdminNavItem } from './layout/AdminShell'

// ─── Utilities ───────────────────────────────────────────────────────────
export { cx } from './utils/cx'
