"""Shared Windows UI typography for Chinese and Latin text."""

from tkinter import font as tkfont


FONT = 'Microsoft YaHei UI'
BODY_SIZE = 10


def configure_typography(root, style):
    """Apply after choosing the ttk theme, before constructing page widgets.

    Explicit page fonts use FONT too. Named fonts cover Tk-built dialogs and
    controls that otherwise inherit a different platform default. Native Windows
    file/message dialogs continue to use the operating system's typography.
    """
    defaults = {
        'TkDefaultFont': (BODY_SIZE, 'normal'),
        'TkTextFont': (BODY_SIZE, 'normal'),
        'TkMenuFont': (BODY_SIZE, 'normal'),
        'TkHeadingFont': (BODY_SIZE, 'bold'),
        'TkCaptionFont': (11, 'bold'),
        'TkSmallCaptionFont': (9, 'normal'),
        'TkIconFont': (BODY_SIZE, 'normal'),
        'TkTooltipFont': (9, 'normal'),
    }
    available = set(tkfont.names(root=root))
    for name, (size, weight) in defaults.items():
        if name in available:
            tkfont.nametofont(name, root=root).configure(family=FONT, size=size, weight=weight)

    root.option_add('*Font', (FONT, BODY_SIZE))
    root.option_add('*TCombobox*Listbox.font', (FONT, BODY_SIZE))
    style.configure('.', font=(FONT, BODY_SIZE))
    body = tkfont.Font(root=root, family=FONT, size=BODY_SIZE)
    row_padding = round(root.winfo_fpixels('6p'))
    style.configure('Treeview', font=(FONT, BODY_SIZE),
                    rowheight=body.metrics('linespace') + row_padding)
    style.configure('Treeview.Heading', font=(FONT, BODY_SIZE, 'bold'))
