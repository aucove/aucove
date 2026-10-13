# .latexmkrc (clase lead). Equivale a la compilación de referencia (02-convencion §4.7).
@default_files = ('tex/main.tex');
$do_cd = 1;
$out_dir = 'build';
$pdf_mode = 1;   # 1 = pdflatex, 5 = xelatex, 4 = lualatex: igual que project.yaml build.engine
set_tex_cmds('%O -no-shell-escape -interaction=nonstopmode -halt-on-error -file-line-error %S');
$ENV{'openout_any'} = 'p';
$ENV{'shell_escape'} = 'f';
