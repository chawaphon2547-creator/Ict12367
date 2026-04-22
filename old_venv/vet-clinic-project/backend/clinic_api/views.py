from django.contrib.auth import authenticate
from django.db.models import Sum, F
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Appointment, InventoryItem, MedicalRecord, Pet, Profile, StaffShift
from .permissions import IsStaff, IsVet, IsOwnerOrStaff
from .serializers import (
    AppointmentSerializer,
    InventorySerializer,
    MedicalRecordSerializer,
    PetSerializer,
    RegisterSerializer,
    UserSerializer,
    StaffShiftSerializer,
)


@api_view(["POST"])
@permission_classes([AllowAny])
def register(request):
    ser = RegisterSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    user = ser.save()
    token, _ = Token.objects.get_or_create(user=user)
    return Response(
        {
            "token": token.key,
            "role": user.profile.role,
            "name": user.profile.display_name,
            "email": user.email,
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def login(request):
    email = (request.data.get("email") or "").strip().lower()
    password = request.data.get("password") or ""
    user = authenticate(request, username=email, password=password)
    if not user:
        return Response(
            {"detail": "อีเมลหรือรหัสผ่านไม่ถูกต้อง"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    token, _ = Token.objects.get_or_create(user=user)
    return Response(
        {
            "token": token.key,
            "role": user.profile.role,
            "name": user.profile.display_name or user.username,
            "email": user.email,
        }
    )


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class StaffListView(APIView):
    permission_classes = [IsAuthenticated, IsStaff]

    def get(self, request):
        from django.contrib.auth.models import User
        staff_users = User.objects.filter(profile__role__in=[Profile.ROLE_VET, Profile.ROLE_ASSISTANT])
        serializer = UserSerializer(staff_users, many=True)
        return Response(serializer.data)


class UserManagementView(APIView):
    permission_classes = [IsAuthenticated, IsStaff]

    def get(self, request):
        # Fetch all users, return serialized details with role
        from django.contrib.auth.models import User
        users = User.objects.all().select_related("profile").order_by("-date_joined")
        serializer = UserSerializer(users, many=True)
        return Response(serializer.data)

    def put(self, request, pk):
        # Update user's role
        from django.contrib.auth.models import User
        try:
            user = User.objects.get(pk=pk)
            new_role = request.data.get("role")
            if new_role not in [Profile.ROLE_CUSTOMER, Profile.ROLE_VET, Profile.ROLE_ASSISTANT]:
                return Response({"detail": "Role ไม่ถูกต้อง"}, status=status.HTTP_400_BAD_REQUEST)
            
            profile = user.profile
            profile.role = new_role
            profile.save()
            return Response(UserSerializer(user).data)
        except User.DoesNotExist:
            return Response({"detail": "ไม่พบผู้ใช้"}, status=status.HTTP_404_NOT_FOUND)

    def delete(self, request, pk):
        # Delete user account
        from django.contrib.auth.models import User
        try:
            user = User.objects.get(pk=pk)
            if user == request.user:
                return Response({"detail": "ไม่สามารถลบตัวเองได้"}, status=status.HTTP_400_BAD_REQUEST)
            user.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except User.DoesNotExist:
            return Response({"detail": "ไม่พบผู้ใช้"}, status=status.HTTP_404_NOT_FOUND)


class BookAppointmentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        pet_id = request.data.get("pet_id")
        scheduled_at = request.data.get("scheduled_at")
        notes = request.data.get("notes", "")
        if not pet_id or not scheduled_at:
            return Response(
                {"detail": "ต้องระบุ pet_id และ scheduled_at"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            pet = Pet.objects.get(pk=pet_id, owner=request.user)
        except Pet.DoesNotExist:
            return Response({"detail": "ไม่พบสัตว์เลี้ยง"}, status=status.HTTP_404_NOT_FOUND)

        appt = Appointment.objects.create(
            pet=pet,
            scheduled_at=scheduled_at,
            status=Appointment.STATUS_PENDING,
            reason=request.data.get("reason", ""),
            notes=notes,
        )
        return Response(AppointmentSerializer(appt).data, status=status.HTTP_201_CREATED)


class StaffShiftViewSet(viewsets.ModelViewSet):
    queryset = StaffShift.objects.all().order_by("date", "start_time")
    serializer_class = StaffShiftSerializer
    permission_classes = [IsAuthenticated, IsStaff]


class PetViewSet(viewsets.ModelViewSet):
    serializer_class = PetSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if getattr(user, "profile", None) and user.profile.role in (Profile.ROLE_VET, Profile.ROLE_ASSISTANT):
            return Pet.objects.all().select_related("owner")
        return Pet.objects.filter(owner=user)

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


class AppointmentViewSet(viewsets.ModelViewSet):
    serializer_class = AppointmentSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = Appointment.objects.select_related("pet", "pet__owner")
        
        specific_date = self.request.query_params.get("date")
        today_only = self.request.query_params.get("today")
        
        if specific_date:
            qs = qs.filter(scheduled_at__date=specific_date)
        elif today_only in ("1", "true", "yes"):
            qs = qs.filter(scheduled_at__date=timezone.localdate())

        if getattr(user, "profile", None) and user.profile.role in (Profile.ROLE_VET, Profile.ROLE_ASSISTANT):
            return qs.order_by("scheduled_at")
        return qs.filter(pet__owner=user).order_by("scheduled_at")

    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated, IsStaff])
    def confirm(self, request, pk=None):
        appt = self.get_object()
        appt.status = Appointment.STATUS_CONFIRMED
        appt.save(update_fields=["status"])
        return Response(AppointmentSerializer(appt).data)

    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated, IsStaff])
    def cancel(self, request, pk=None):
        appt = self.get_object()
        appt.status = Appointment.STATUS_CANCELLED
        appt.save(update_fields=["status"])
        return Response(AppointmentSerializer(appt).data)


class InventoryViewSet(viewsets.ModelViewSet):
    queryset = InventoryItem.objects.all().order_by("name")
    serializer_class = InventorySerializer
    permission_classes = [IsAuthenticated, IsStaff]

    def get_permissions(self):
        if self.action in ("create", "update", "partial_update", "destroy"):
            # เฉพาะหมอที่แก้ไขคลังได้ (ตามความต้องการในจุดกำหนดยา/ราคา)
            return [IsAuthenticated(), IsVet()]
        return super().get_permissions()


class MedicalRecordViewSet(viewsets.ModelViewSet):
    serializer_class = MedicalRecordSerializer
    permission_classes = [IsAuthenticated, IsStaff]

    def get_queryset(self):
        qs = MedicalRecord.objects.select_related("appointment", "appointment__pet").prefetch_related("prescription_lines__inventory_item")
        pet_id = self.request.query_params.get("pet_id")
        if pet_id:
            qs = qs.filter(appointment__pet_id=pet_id)
        return qs.order_by("-created_at")

    @action(detail=True, methods=["post"], permission_classes=[IsStaff])
    def pay(self, request, pk=None):
        record = self.get_object()
        
        new_amount = request.data.get("amount")
        if new_amount is not None:
            try:
                record.total_amount = float(new_amount)
            except ValueError:
                pass
                
        record.is_paid = True
        record.save(update_fields=["is_paid", "total_amount"])
        return Response({"status": "paid", "total": record.total_amount})


class FinancialReportView(APIView):
    permission_classes = [IsAuthenticated, IsVet]

    def get(self, request):
        days = int(request.query_params.get("days", 7))
        start_date = timezone.now() - timezone.timedelta(days=days)

        # 1. Calculate overall stats (Separately to avoid double-counting from JOINs)
        revenue_stats = MedicalRecord.objects.filter(
            created_at__gte=start_date, is_paid=True
        ).aggregate(
            total_rev=Sum('total_amount')
        )
        
        cost_stats = PrescriptionLine.objects.filter(
            medical_record__created_at__gte=start_date,
            medical_record__is_paid=True
        ).aggregate(
            total_cost=Sum(F('unit_cost') * F('quantity'))
        )

        total_revenue = float(revenue_stats['total_rev'] or 0)
        total_cost = float(cost_stats['total_cost'] or 0)

        # 2. Group by medicine (Medicine Sales Summary)
        medicine_sales_qs = PrescriptionLine.objects.filter(
            medical_record__created_at__gte=start_date,
            medical_record__is_paid=True
        ).values(
            name=F('inventory_item__name')
        ).annotate(
            qty=Sum('quantity'),
            rev=Sum(F('unit_price') * F('quantity'))
        ).order_by('-rev')

        medicine_sales = {}
        for item in medicine_sales_qs:
            medicine_sales[item['name']] = {"qty": item['qty'], "rev": float(item['rev'])}

        return Response({
            "period_days": days,
            "total_revenue": total_revenue,
            "total_cost": total_cost,
            "net_profit": total_revenue - total_cost,
            "medicine_summary": medicine_sales
        })
