/* Test implementation, written for this project. No driver code is included.
 * CIFX_SDK_HEADERS compiles the same shim against pinned official SDK headers;
 * otherwise the independently declared ABI below keeps offline CI self-contained.
 */
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdatomic.h>
#include <time.h>
#ifdef VIRTETH_FIXTURE
int32_t cifx_cifxeth_scan(char* board) {(void)board; return 0;}
#endif
#ifdef TIME_FIXTURE
void cifXInitTime(void* device) {(void)device;}
#endif
#ifdef DMA_FIXTURE
int USER_GetDMAMode(void* device) {(void)device; return 0;}
#endif
#ifdef CIFX_SDK_HEADERS
#include "cifxlinux.h"
#else
typedef void* CIFXHANDLE;
typedef struct {uint32_t ulSize, ulPhysicalAddress; void* pvBuffer;} DMABUFFER_T;
struct CIFX_DEVICE_T {
  unsigned char* dpm; unsigned long dpmaddr, dpmlen;
  int uio_num,uio_fd,pci_card,force_ram;
  void *notify,*userparam; unsigned char *extmem;
  unsigned long extmemaddr,extmemlen;
  uint32_t dma_buffer_cnt; DMABUFFER_T dma_buffer[8];
  void *hwif_init,*hwif_deinit,*hwif_read,*hwif_write;
};
struct CIFX_LINUX_INIT {
  int init_options; const char* base_dir; unsigned long poll_interval;
  int poll_priority; unsigned long trace_level; int user_card_cnt;
  struct CIFX_DEVICE_T* user_cards; int iCardNumber,fEnableCardLocking,poll_StackSize,poll_schedpolicy;
  void* logfd;
};
typedef struct __attribute__((packed)) {
  char abBoardName[16],abBoardAlias[16]; uint32_t ulDeviceNumber,ulSerialNumber;
  uint16_t usFWMajor,usFWMinor,usFWBuild,usFWRevision;
  uint8_t bFWNameLength,abFWName[63]; uint16_t usFWYear; uint8_t bFWMonth,bFWDay;
  uint32_t ulChannelError,ulOpenCnt,ulPutPacketCnt,ulGetPacketCnt,ulMailboxSize;
  uint32_t ulIOInAreaCnt,ulIOOutAreaCnt,ulHskSize,ulNetxFlags,ulHostFlags,ulHostCOSFlags,ulDeviceCOSFlags;
} CHANNEL_INFORMATION;
typedef struct __attribute__((packed)) {uint32_t ulTotalSize,ulReserved,ulIOMode;} CHANNEL_IO_INFORMATION;
#endif
static _Atomic int state[64];
static struct CIFX_DEVICE_T device;
static uint8_t outputs[64];
static void block(int flag,int entered) {
  state[entered]=1;
  while(state[flag] && !state[21]) {struct timespec ts={0,1000000}; nanosleep(&ts,0);}
}
void shim_reset(void) {
  memset(&device,0,sizeof(device)); memset(outputs,0,sizeof(outputs));
  for(int n=0;n<64;n++) state[n]=0;
  state[10]=128;state[11]=128;state[12]=1;state[13]=1;
  state[30]=5678;state[31]=1;state[32]=4;state[33]=64;state[34]=1;state[35]=2;
  device.uio_num=7;device.uio_fd=42;
}
void shim_set(int key,int value) {state[key]=value;}
int shim_get(int key) {return state[key];}
int shim_output(int index) {return outputs[index];}
size_t shim_layout(int n) {
  const size_t values[]={sizeof(struct CIFX_LINUX_INIT),sizeof(struct CIFX_DEVICE_T),
    sizeof(CHANNEL_INFORMATION),sizeof(CHANNEL_IO_INFORMATION),sizeof(DMABUFFER_T),
    offsetof(struct CIFX_LINUX_INIT,user_cards),offsetof(struct CIFX_LINUX_INIT,logfd),
    offsetof(struct CIFX_DEVICE_T,uio_num),offsetof(struct CIFX_DEVICE_T,force_ram),
    offsetof(struct CIFX_DEVICE_T,dma_buffer),offsetof(struct CIFX_DEVICE_T,hwif_write),
    offsetof(CHANNEL_INFORMATION,bFWNameLength),offsetof(CHANNEL_INFORMATION,ulChannelError),
    offsetof(CHANNEL_INFORMATION,ulDeviceCOSFlags)};
  return values[n];
}
struct CIFX_DEVICE_T* cifXFindDevice(int card,int locking) {
  state[5]++; if(card!=2 || locking!=1) return 0; return &device;
}
void cifXDeleteDevice(struct CIFX_DEVICE_T* p) {if(p==&device) state[3]++;}
int32_t cifXDriverInit(const struct CIFX_LINUX_INIT* p) {
  state[2]++;
  if(p->init_options!=0 || p->user_card_cnt!=1 || p->user_cards!=&device || p->fEnableCardLocking!=1 || !p->logfd) return -1;
  block(38,39); return state[37];
}
void cifXDriverDeinit(void) {state[4]++;}
int32_t xDriverOpen(CIFXHANDLE* p) {*p=(void*)123;return 0;}
int32_t xDriverClose(CIFXHANDLE p) {state[40]++;return p==(void*)123?0:-1;}
int32_t xChannelOpen(CIFXHANDLE p,char* board,uint32_t channel,CIFXHANDLE* out) {
  if(p!=(void*)123 || strcmp(board,"cifX0") || channel!=0) return -1;
  *out=(void*)456;return 0;
}
int32_t xChannelClose(CIFXHANDLE p) {state[41]++;return p==(void*)456?0:-1;}
int32_t xChannelInfo(CIFXHANDLE p,uint32_t size,void* buffer) {
  if(p!=(void*)456 || size!=sizeof(CHANNEL_INFORMATION)) return -1;
  CHANNEL_INFORMATION* q=buffer; memset(q,0,sizeof(*q));
  q->ulDeviceNumber=1234; q->ulSerialNumber=state[30];
  q->usFWMajor=3;q->usFWMinor=state[35];q->bFWNameLength=22;
  memcpy(q->abFWName,"PROFINET IO Controller",22);
  q->ulChannelError=state[36];q->ulOpenCnt=state[34];q->ulIOInAreaCnt=1;q->ulIOOutAreaCnt=1;
  q->ulNetxFlags=1;q->ulDeviceCOSFlags=7|state[42];return 0;
}
int32_t xChannelBusState(CIFXHANDLE p,uint32_t command,uint32_t* bus,uint32_t timeout) {
  if(command!=2) {state[6]++;return -1;}
  *bus=state[31];return p==(void*)456 && timeout==0?0:-1;
}
int32_t xChannelIOInfo(CIFXHANDLE p,uint32_t direction,uint32_t area,uint32_t size,void* out) {
  if(p!=(void*)456 || area!=0 || size!=sizeof(CHANNEL_IO_INFORMATION))return -1;
  CHANNEL_IO_INFORMATION* q=out;q->ulReserved=0;q->ulIOMode=state[32];
  q->ulTotalSize=direction==1?state[33]:64;return direction==1 || direction==2?0:-1;
}
int32_t xChannelIORead(CIFXHANDLE p,uint32_t area,uint32_t offset,uint32_t length,void* out,uint32_t timeout) {
  state[1]++;block(17,19);
  if(p!=(void*)456 || area!=0 || offset!=0 || length!=64 || timeout==0)return -1;
  uint8_t* q=out;memset(q,0,length);
  q[0]=state[1]%2?0xaa:1;q[1]=2;q[2]=state[10];q[3]=state[11];
  q[16]=state[12];q[32]=state[13];q[48]=state[14];return state[15];
}
int32_t xChannelIOWrite(CIFXHANDLE p,uint32_t area,uint32_t offset,uint32_t length,void* data,uint32_t timeout) {
  state[0]++;block(18,20);
  if(p!=(void*)456 || area!=0 || offset!=0 || length!=3 || timeout==0)return -1;
  memcpy(outputs+offset,data,length);return state[16];
}
