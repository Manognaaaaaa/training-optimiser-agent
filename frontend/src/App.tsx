import { Navigate, Route, Routes } from 'react-router-dom'
import AppLayout from './layouts/AppLayout'
import Alerts from './pages/Alerts'
import AuditLog from './pages/AuditLog'
import Calendar from './pages/Calendar'
import CourseDetail from './pages/CourseDetail'
import Dashboard from './pages/Dashboard'
import Fairness from './pages/Fairness'
import { CoursesPage, DriversPage, SessionsPage, TrainersPage } from './pages/Records'
import Simulation from './pages/Simulation'

export default function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route index element={<Dashboard />} />
        <Route path="dashboard/courses/:id" element={<CourseDetail />} />
        <Route path="drivers" element={<DriversPage />} />
        <Route path="trainers" element={<TrainersPage />} />
        <Route path="courses" element={<CoursesPage />} />
        <Route path="sessions" element={<SessionsPage />} />
        <Route path="master-data" element={<Navigate to="/drivers" replace />} />
        <Route path="calendar" element={<Calendar />} />
        <Route path="alerts" element={<Alerts />} />
        <Route path="audit-log" element={<AuditLog />} />
        <Route path="fairness" element={<Fairness />} />
        <Route path="simulation" element={<Simulation />} />
      </Route>
    </Routes>
  )
}
