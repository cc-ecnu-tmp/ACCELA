#ifndef ACCELA_BENCH_SYSY_RUNTIME_H
#define ACCELA_BENCH_SYSY_RUNTIME_H

int getint(void);
int getch(void);
int getarray(void *array);
float getfloat(void);
int getfarray(void *array);

void putint(int value);
void putch(int value);
void putarray(int count, void *array);
void putfloat(float value);
void putfarray(int count, void *array);
void putf(char format[], ...);

void _sysy_starttime(int line);
void _sysy_stoptime(int line);

#define starttime() _sysy_starttime(__LINE__)
#define stoptime() _sysy_stoptime(__LINE__)

#endif
