import './globals.css';

export const metadata = {
  title: 'News Pipeline Telemetry',
  description: 'Execution logs and extracted stories from the automated news pipeline',
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
