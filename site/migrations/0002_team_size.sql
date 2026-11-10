-- Team-page signups say how big the team is (1-10 | 11-30 | 31-100); individuals leave it empty.
ALTER TABLE waitlist ADD COLUMN team_size TEXT;
