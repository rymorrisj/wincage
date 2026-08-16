#define SDL_MAIN_HANDLED
// Tests OpenGL 4.5 core context creation via WGL inside an AppContainer.
// Exits 0 on PASS, 1 on FAIL. Prints one status line to stdout.
#include <SDL.h>
#include <iostream>

int main() {
    SDL_SetMainReady();

    if (SDL_Init(SDL_INIT_VIDEO) != 0) {
        std::cout << "FAIL: SDL_Init: " << SDL_GetError() << "\n";
        return 1;
    }

    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION, 4);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION, 5);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_PROFILE_MASK, SDL_GL_CONTEXT_PROFILE_CORE);

    SDL_Window* win = SDL_CreateWindow(
        "sdl2_opengl_check",
        SDL_WINDOWPOS_UNDEFINED, SDL_WINDOWPOS_UNDEFINED,
        320, 240,
        SDL_WINDOW_OPENGL | SDL_WINDOW_HIDDEN
    );
    if (!win) {
        std::cout << "FAIL: SDL_CreateWindow (OPENGL): " << SDL_GetError() << "\n";
        SDL_Quit();
        return 1;
    }

    SDL_GLContext ctx = SDL_GL_CreateContext(win);
    if (!ctx) {
        std::cout << "FAIL: SDL_GL_CreateContext (OpenGL 4.5 core): " << SDL_GetError() << "\n";
        SDL_DestroyWindow(win);
        SDL_Quit();
        return 1;
    }

    SDL_GL_DeleteContext(ctx);
    SDL_DestroyWindow(win);
    SDL_Quit();

    std::cout << "PASS: OpenGL 4.5 core context created successfully\n";
    return 0;
}
