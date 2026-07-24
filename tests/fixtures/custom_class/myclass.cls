\NeedsTeXFormat{LaTeX2e}
\ProvidesClass{myclass}[2024/01/01 Custom Test Class]

\LoadClass[a4paper, 12pt]{article}

\usepackage{geometry}
\geometry{margin=2.5cm}

% Custom command
\newcommand{\mytitle}[1]{%
  \begin{center}
    {\Large\bfseries #1}\\[1em]
  \end{center}
}

\endinput
