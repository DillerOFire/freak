{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  buildInputs = with pkgs; [
    python311
    uv
    ffmpeg
    git
    just
  ];

  shellHook = ''
    # Set up PYTHONPATH so imports work correctly
    export PYTHONPATH=".:$PYTHONPATH"

    # Set up LD_LIBRARY_PATH for dynamically linked libraries needed by python packages (like opencv)
    export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath [
      pkgs.stdenv.cc.cc.lib
      pkgs.zlib
      pkgs.glib
      pkgs.libGL
      pkgs.libx11
      pkgs.libxext
      pkgs.libxrender
    ]}:$LD_LIBRARY_PATH"

    echo "=== Freak Bot Dev Shell ==="
    echo "Dependencies: Python 3.11, uv, ffmpeg, git, just"
    echo "Run 'just' to see available commands."
    echo "==========================="
  '';
}
