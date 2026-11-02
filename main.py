"""Desktop entry point. Keep frozen child processes out of the GUI startup path."""

if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    from jarvis_camera.app import main
    main()
