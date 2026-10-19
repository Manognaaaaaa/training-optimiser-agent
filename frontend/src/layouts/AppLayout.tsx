import * as Dialog from '@radix-ui/react-dialog'
import { useQuery } from '@tanstack/react-query'
import {
  BookOpen, CalendarClock, CalendarDays, Clock, GraduationCap, LayoutDashboard, Menu, PanelLeftClose, PanelLeftOpen, Bell, ScrollText, Scale, Users, X,
  type LucideIcon,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, Outlet, useLocation } from 'react-router-dom'
import { getAlertCounts } from '../api/alerts'
import ApiStatus from '../components/ApiStatus'
import { SimClock } from '../components/shell/SimClock'
import { cn } from '../lib/cn'

interface NavItem {
  to: string
  label: string
  Icon: LucideIcon
  /** Where this item counts as "current": a pathname, plus an optional ?tab= value. */
  match: (pathname: string, search: URLSearchParams) => boolean
  badge?: 'alerts'
}

const GROUPS: { title: string; items: NavItem[] }[] = [
  {
    title: 'Overview',
    items: [
      { to: '/', label: 'Dashboard', Icon: LayoutDashboard, match: (p) => p === '/' || p.startsWith('/dashboard') },
      { to: '/alerts', label: 'Alerts', Icon: Bell, match: (p) => p.startsWith('/alerts'), badge: 'alerts' },
      { to: '/calendar', label: 'Calendar', Icon: CalendarDays, match: (p) => p.startsWith('/calendar') },
    ],
  },
  {
    title: 'Records',
    items: [
      { to: '/drivers', label: 'Drivers', Icon: Users, match: (p) => p.startsWith('/drivers') },
      { to: '/trainers', label: 'Trainers', Icon: GraduationCap, match: (p) => p.startsWith('/trainers') },
      { to: '/courses', label: 'Courses', Icon: BookOpen, match: (p) => p.startsWith('/courses') },
      { to: '/sessions', label: 'Sessions', Icon: CalendarClock, match: (p) => p.startsWith('/sessions') },
    ],
  },
  {
    title: 'Oversight',
    items: [
      { to: '/audit-log', label: 'Audit log', Icon: ScrollText, match: (p) => p.startsWith('/audit-log') },
      { to: '/fairness', label: 'Fairness', Icon: Scale, match: (p) => p.startsWith('/fairness') },
    ],
  },
  {
    title: 'Settings',
    items: [{ to: '/simulation', label: 'Simulation', Icon: Clock, match: (p) => p.startsWith('/simulation') }],
  },
]

/** Section / page names for the breadcrumb in the top bar. */
function breadcrumbFor(pathname: string, search: URLSearchParams): string[] {
  for (const group of GROUPS) {
    for (const item of group.items) {
      if (item.match(pathname, search)) {
        const trail = [group.title, item.label]
        if (pathname.startsWith('/dashboard/courses/')) trail.push('Course')
        return trail
      }
    }
  }
  return ['Training Optimiser']
}

function NavList({ collapsed, onNavigate }: { collapsed: boolean; onNavigate?: () => void }) {
  const location = useLocation()
  const search = new URLSearchParams(location.search)
  const counts = useQuery({ queryKey: ['alerts', 'counts'], queryFn: getAlertCounts, refetchInterval: 10_000 })
  const openAlerts = counts.data?.plans_ready ?? 0

  return (
    <nav aria-label="Main" className="flex-1 space-y-5 overflow-y-auto px-3 py-2">
      {GROUPS.map((group) => (
        <div key={group.title}>
          {!collapsed && <div className="px-3 pb-1 text-xs font-medium text-ink-muted">{group.title}</div>}
          <ul className="space-y-0.5">
            {group.items.map(({ to, label, Icon, match, badge }) => {
              const current = match(location.pathname, search)
              return (
                <li key={to}>
                  <Link
                    to={to}
                    onClick={onNavigate}
                    title={collapsed ? label : undefined}
                    aria-current={current ? 'page' : undefined}
                    className={cn(
                      'flex items-center gap-3 rounded-control px-3 py-2 text-sm font-medium',
                      current ? 'bg-primary text-white' : 'text-ink hover:bg-canvas',
                      collapsed && 'justify-center px-2',
                    )}
                  >
                    <Icon size={18} aria-hidden="true" className="shrink-0" />
                    {!collapsed && <span className="flex-1">{label}</span>}
                    {badge === 'alerts' && openAlerts > 0 && (
                      <span
                        className={cn(
                          'rounded-full px-1.5 py-0.5 text-xs font-semibold',
                          current ? 'bg-white text-primary' : 'bg-risk-high text-white',
                          collapsed && 'absolute ml-5 -mt-5 px-1 text-[10px]',
                        )}
                        aria-label={`${openAlerts} alerts with plans ready`}
                      >
                        {openAlerts}
                      </span>
                    )}
                  </Link>
                </li>
              )
            })}
          </ul>
        </div>
      ))}
    </nav>
  )
}

function Brand({ collapsed }: { collapsed: boolean }) {
  return (
    <Link to="/" className={cn('flex items-center gap-2 px-5 py-4 font-heading text-xl font-bold text-primary', collapsed && 'justify-center px-2')}>
      {collapsed ? 'TO' : 'Training Optimiser'}
    </Link>
  )
}

export default function AppLayout() {
  const location = useLocation()
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem('nav-collapsed') === '1'
    } catch {
      return false
    }
  })
  const [mobileOpen, setMobileOpen] = useState(false)

  useEffect(() => {
    try {
      localStorage.setItem('nav-collapsed', collapsed ? '1' : '0')
    } catch {
      /* storage can be blocked; the menu simply will not be remembered */
    }
  }, [collapsed])

  const trail = breadcrumbFor(location.pathname, new URLSearchParams(location.search))

  return (
    <div className="flex min-h-screen bg-canvas text-ink">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded-control focus:bg-surface focus:px-3 focus:py-2">
        Skip to content
      </a>

      <aside className={cn('hidden shrink-0 flex-col border-r border-line bg-surface md:flex', collapsed ? 'w-16' : 'w-60')}>
        <Brand collapsed={collapsed} />
        <NavList collapsed={collapsed} />
        <button
          type="button"
          onClick={() => setCollapsed((c) => !c)}
          aria-label={collapsed ? 'Expand the menu' : 'Collapse the menu'}
          className="m-3 flex items-center justify-center gap-2 rounded-control px-3 py-2 text-sm text-ink-muted hover:bg-canvas hover:text-ink"
        >
          {collapsed ? <PanelLeftOpen size={18} aria-hidden="true" /> : <><PanelLeftClose size={18} aria-hidden="true" /> Collapse</>}
        </button>
      </aside>

      <Dialog.Root open={mobileOpen} onOpenChange={setMobileOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-40 bg-ink/40 md:hidden" />
          <Dialog.Content className="fixed inset-y-0 left-0 z-50 flex w-64 flex-col bg-surface shadow-2xl md:hidden">
            <Dialog.Title className="sr-only">Menu</Dialog.Title>
            <Dialog.Description className="sr-only">Main navigation</Dialog.Description>
            <div className="flex items-center justify-between pr-3">
              <Brand collapsed={false} />
              <Dialog.Close className="rounded-control p-2 text-ink-muted hover:bg-canvas" aria-label="Close the menu">
                <X size={18} aria-hidden="true" />
              </Dialog.Close>
            </div>
            <NavList collapsed={false} onNavigate={() => setMobileOpen(false)} />
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex flex-wrap items-center gap-3 border-b border-line bg-surface px-4 py-2 md:px-6">
          <button
            type="button"
            className="rounded-control p-2 text-ink hover:bg-canvas md:hidden"
            aria-label="Open the menu"
            onClick={() => setMobileOpen(true)}
          >
            <Menu size={20} aria-hidden="true" />
          </button>
          <nav aria-label="Breadcrumb" className="min-w-0 flex-1 truncate text-sm text-ink-muted">
            {trail.map((part, i) => (
              <span key={part + i}>
                {i > 0 && <span aria-hidden="true"> / </span>}
                <span className={i === trail.length - 1 ? 'font-medium text-ink' : undefined}>{part}</span>
              </span>
            ))}
          </nav>
          <div className="order-last w-full md:order-none md:w-auto">
            <SimClock />
          </div>
          <div className="md:order-last">
            <ApiStatus />
          </div>
        </header>
        <main id="main" className="flex-1 p-4 md:p-8">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
