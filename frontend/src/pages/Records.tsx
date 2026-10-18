import type { ReactNode } from 'react'
import CoursesTab from '../components/master/CoursesTab'
import DriversTab from '../components/master/DriversTab'
import SessionsTab from '../components/master/SessionsTab'
import TrainersTab from '../components/master/TrainersTab'
import { PageHeader } from '../components/ui'

function RecordsPage({ title, lead, children }: { title: string; lead: string; children: ReactNode }) {
  return (
    <div className="space-y-6">
      <PageHeader title={title} lead={lead} />
      {children}
    </div>
  )
}

export const DriversPage = () => (
  <RecordsPage title="Drivers" lead="The people who take training. Search or filter, then click a row to see their bookings, history and time off.">
    <DriversTab />
  </RecordsPage>
)

export const TrainersPage = () => (
  <RecordsPage title="Trainers" lead="Who teaches the sessions, and how busy each trainer is this week.">
    <TrainersTab />
  </RecordsPage>
)

export const CoursesPage = () => (
  <RecordsPage title="Courses" lead="Each course with its yearly completion target. Changing a target changes the forecasts.">
    <CoursesTab />
  </RecordsPage>
)

export const SessionsPage = () => (
  <RecordsPage title="Sessions" lead="Every training session, past and planned. Click a row to see who is booked and to add or remove drivers.">
    <SessionsTab />
  </RecordsPage>
)
