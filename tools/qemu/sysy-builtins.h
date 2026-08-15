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

#define starttime() _sysy_starttime(__LINE__)
#define stoptime() _sysy_stoptime(__LINE__)
