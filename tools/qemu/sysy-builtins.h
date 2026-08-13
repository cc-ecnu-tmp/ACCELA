#ifndef ACCELA_SYSY_BUILTINS_H
#define ACCELA_SYSY_BUILTINS_H

#ifdef __cplusplus
extern "C" {
#endif

int main(void);
int getint(void);
int getch(void);
float getfloat(void);
int getarray(void *);
int getfarray(void *);
void putint(int);
void putch(int);
void putfloat(float);
void putarray(int, const void *);
void putfarray(int, const void *);
void _sysy_starttime(int);
void _sysy_stoptime(int);

#ifdef __cplusplus
}
#endif

#define starttime() _sysy_starttime(__LINE__)
#define stoptime() _sysy_stoptime(__LINE__)

#endif
