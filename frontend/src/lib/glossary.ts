/** Short plain-language explanations, shown by <InfoTip term="..."> next to any metric that needs one. */
export const GLOSSARY = {
  vs_schedule: 'Compares completions so far with where the course should be by today if it were on a steady pace to the yearly target.',
  expected_by: 'Our best estimate of total completions by 31 December, based on the sessions still on the calendar. The "likely" range is where the real number should land 9 times out of 10.',
  chance: 'How likely the course is to reach its yearly target, given its bookings, free seats and how often booked drivers really turn up.',
  problem: 'The main reason the course may miss its target: too few seats, drivers not turning up, or too few drivers left who still need the course.',
  target_pace: 'Where the course should be today if it finished exactly on target by 31 December. The line on each progress bar marks it.',
  show_rate: 'Out of the drivers booked into a session, the share who actually attend.',
  seats_left: 'Seats still open in sessions that have not happened yet.',
  eligible_pool: 'Active drivers who have not yet completed this course this year.',
  mandatory: 'Courses every driver must complete. Their yearly targets are the ones that matter most for compliance.',
  shortfall: 'How many completions the course is expected to be short of its yearly target.',
  sim_clock: 'This app runs on a simulated calendar. Moving the clock forward plays out bookings and attendance for those days.',
} as const

export type GlossaryTerm = keyof typeof GLOSSARY
